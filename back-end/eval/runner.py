"""评估执行器。

一次运行的形状：

    对每个配置变体：
        （分块配置变了就重建索引）
        对每个问题：
            检索 -> 算检索指标
            生成答案 -> LLM 裁判打分
            从 trace 里取出本题的 token / 成本 / 耗时
        汇总成一行

关键取舍：这里评的是 **RAG 问答链路**（检索 + 单次生成），不是完整的 Agent 循环。
Agent 循环涉及多轮工具决策，方差大、成本高，适合单独设一套端到端评估；
把两者混在一起会让「检索改动到底有没有用」变得无法归因。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from config import settings
from database import SessionLocal
from eval import metrics
from eval.corpus_degrade import degrade_corpus_file
from eval.judge import AnswerJudge, JudgeVerdict
from eval.rate_gate import LLMRateGate
from eval.variants import Variant
from models import Document, User
from services.knowledge_service import KnowledgeService
from services.model_adapter import OpenAICompatibleAdapter
from services.pricing import estimate_cost
from services import prompt_library
from services.retrieval_index import invalidate_scope_indexes
from services.telemetry import tracer

logger = logging.getLogger("eval.runner")

_EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
CORPUS_DIR = os.path.join(_EVAL_DIR, "corpus")
DATASET_PATH = os.path.join(_EVAL_DIR, "datasets", "rag_golden.jsonl")

# 评估语料挂在这个固定的伪用户下，和真实用户数据完全隔离
EVAL_USER_ID = "eval-harness"

# 生成被评回答那一次调用的输出预算。
#
# 原来是写死的 800，而 2026-08-22 加上 finish_reason 之后立刻看到它在真实运行里
# 返回**空串**:推理模型先花预算思考，800 不够时一个字都不吐。后果比别处更严重
# ——空答案会被裁判判成失败，于是报告上呈现为"这个变体答不出来"，而模型其实
# 从没得到过说话的机会。尺子自己坏在了最不容易怀疑的地方。
#
# 给得比 _JUDGE_MAX_TOKENS 小一点是有意的:回答要的是几百字散文，裁判要的是
# 一个带 reason 的 JSON，后者更容易被思考挤掉。
_ANSWER_MAX_TOKENS = 3072


# 单例:一个评估进程里所有变体共用同一把锁,配额是账号级的,不是变体级的。
_RATE_GATE = LLMRateGate()


def ensure_eval_user(session: Any) -> None:
    """确保评估用的伪用户在 ``users`` 表里存在。

    ``documents.user_id`` 与 ``chats.user_id`` 都是指向 ``users.id`` 的外键，
    InnoDB 会真的去校验——挂一个不存在的 user_id 会直接被拒，而报错发生在
    上传语料那一步，看起来像"知识库坏了"，很难联想到是评估的伪用户没建。

    密码哈希取一个随机串现算，算完就丢：这个账号因此永远登不进去。
    在库里留一个口令固定的账号，比外键报错糟得多。``is_active=False`` 是第二道。
    """
    from auth import get_password_hash

    from services import workspace_service

    user = session.query(User).filter(User.id == EVAL_USER_ID).first()
    if user is None:
        session.add(
            User(
                id=EVAL_USER_ID,
                email="eval-harness@invalid.local",
                name="离线评估专用（非真实用户）",
                hashed_password=get_password_hash(secrets.token_urlsafe(32)),
                provider="local",
                is_active=False,
                is_verified=False,
            )
        )
        session.commit()
        user = session.query(User).filter(User.id == EVAL_USER_ID).first()
        logger.info("created eval harness user %s", EVAL_USER_ID)
    # 语料挂在 eval 用户的工作区上,检索按工作区过滤——与线上同一套作用域逻辑
    workspace_service.resolve_for_user(session, user)

# 回答提示词的正文在 prompts/eval_rag_answer/<version>.md，版本由
# settings.PROMPT_EVAL_ANSWER_VERSION 决定，因此可以作为变体维度被扫。
ANSWER_PROMPT_KEY = "eval_rag_answer"


@dataclass(slots=True)
class QuestionCase:
    id: str
    question: str
    expected_documents: list[str]
    reference_answer: str = ""
    must_include: list[str] = field(default_factory=list)
    # 命中即算失败的字符串。注入类样本靠它判定：只要 canary 出现在回答里，
    # 就说明模型执行了资料里夹带的指令。
    must_avoid: list[str] = field(default_factory=list)
    probe: str = "general"
    answerable: bool = True


@dataclass(slots=True)
class QuestionResult:
    case: QuestionCase
    retrieved_documents: list[str]
    retrieval: dict[str, float]
    answer: str
    keyword_coverage: float
    # 回答里出现了几个 must_avoid 字符串。>0 就是护栏没兜住。
    avoid_hits: int
    verdict: JudgeVerdict
    prompt_tokens: int
    completion_tokens: int
    cost: float | None
    currency: str | None
    # 有 token 但价目表命中不了的模型名。空集才代表成本列是完整的。
    unpriced_models: set[str]
    # 降级了的检索增强阶段，同一阶段多次降级就出现多次。
    degraded_stages: list[str]
    latency_ms: int
    retrieval_ms: int
    # 每次降级的原因，形如 ``rerank:truncated``。带默认值所以放在末尾——
    # dataclass 不允许有默认值的字段排在没默认值的前面。
    degraded_reasons: list[str] = field(default_factory=list)


def load_cases(
    limit: int | None = None, dataset_path: str | None = None
) -> list[QuestionCase]:
    cases: list[QuestionCase] = []
    with open(dataset_path or DATASET_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            cases.append(
                QuestionCase(
                    id=raw["id"],
                    question=raw["question"],
                    expected_documents=list(raw.get("expected_documents") or []),
                    reference_answer=raw.get("reference_answer", ""),
                    must_include=list(raw.get("must_include") or []),
                    must_avoid=list(raw.get("must_avoid") or []),
                    probe=raw.get("probe", "general"),
                    answerable=bool(raw.get("answerable", True)),
                )
            )
    return cases[:limit] if limit else cases


def _corpus_digest() -> str:
    """语料文件名 + 内容的摘要。

    改一篇语料的正文而不动任何配置，是完全正常的操作（拆一节、补一句、修个
    错别字）。但 ``ensure_corpus`` 的早退条件只看"篇数对上了、没有陈旧文档"，
    于是这种改动**不会**触发重新索引：库里躺着改动前的分块，报告出来的数字属于
    旧语料，而且没有任何迹象说明这一点。

    2026-08-23 把 ``## 账号与口令`` 拆成两节时踩到：拆分本身让 rerank 分从
    0.0142 涨到 0.2124，但只改文件的话 eval 一个字都不会变。

    并进指纹后，语料内容变了就自动表现为"另一批文档"，走和换分块配置同一条
    重建路径。代价是改一篇要重嵌全部——分块级增量需要按篇存指纹，那是另一层
    设计，而全量重嵌当前只有 92 个分块。
    """
    digest = hashlib.sha256()
    for name in sorted(os.listdir(CORPUS_DIR)):
        if not name.endswith(".md"):
            continue
        digest.update(name.encode("utf-8"))
        with open(os.path.join(CORPUS_DIR, name), "rb") as handle:
            digest.update(handle.read())
    return digest.hexdigest()


def _chunking_fingerprint() -> str:
    """分块结果只由这几个设置决定；它们不变就不必重新索引。

    ``corpus_degrade`` 也在里面：降级方式换了，语料内容就换了，而它不是
    "分块配置"却同样决定了库里躺着什么。不并进指纹的话第二个脏语料变体会命中
    上一个变体留下的索引——测的是上一次的配置，正是文档名带指纹这个设计要防的
    那类静默错误。
    """
    payload = json.dumps(
        {
            "chunk_max": settings.CHUNK_MAX_TOKENS,
            "chunk_overlap": settings.CHUNK_OVERLAP_TOKENS,
            "counter": settings.TOKEN_COUNTER,
            "embedding": settings.EMBEDDING_MODEL,
            "degrade": settings.EVAL_CORPUS_DEGRADE,
            # 清洗开关也进指纹:dirty-pdf-like 和 dirty-pdf-like+clean 用的是
            # 同一份脏语料,但清洗后落库的正文完全不同
            "clean": settings.INGEST_CLEAN,
            "pdf_structure": settings.INGEST_PDF_STRUCTURE,
            # 语料正文本身。见 _corpus_digest：改语料不改配置是常规操作，
            # 而漏掉它会让 eval 静默地测旧索引。
            "corpus": _corpus_digest(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


async def ensure_corpus(knowledge: KnowledgeService) -> tuple[int, bool]:
    """确保评估语料已按当前分块配置与降级方式索引好。

    文档名里带上指纹，于是「换了分块配置」或「换了降级方式」自动表现为
    「另一批文档」：旧的删掉、新的重建，不会出现「测的是上一个配置留下的索引」
    这种静默错误。返回 (分块总数, 是否重建过)。

    金标准按 ``expected_documents`` 里的**原始文件名**标注，所以降级换掉的后缀
    要在文档名里保留原名 + 新后缀（``hr-handbook.md.txt#指纹``），由
    ``_document_label`` 还原回去。
    """
    fingerprint = _chunking_fingerprint()
    degrade = settings.EVAL_CORPUS_DEGRADE
    session = SessionLocal()
    try:
        ensure_eval_user(session)
        eval_user = session.query(User).filter(User.id == EVAL_USER_ID).first()
        existing = (
            session.query(Document)
            .filter(Document.workspace_id == eval_user.workspace_id)
            .all()
        )
        current = [doc for doc in existing if doc.name.endswith(f"#{fingerprint}")]
        stale = [doc for doc in existing if not doc.name.endswith(f"#{fingerprint}")]

        files = sorted(
            name for name in os.listdir(CORPUS_DIR) if name.endswith(".md")
        )
        indexed_ok = [doc for doc in current if doc.status == "indexed"]
        if len(indexed_ok) == len(files) and not stale:
            return sum(doc.chunks for doc in indexed_ok), False

        for doc in stale + [doc for doc in current if doc.status != "indexed"]:
            session.delete(doc)
        session.commit()
        invalidate_scope_indexes(eval_user.workspace_id)

        total_chunks = 0
        rejected: list[str] = []
        for name in files:
            with open(os.path.join(CORPUS_DIR, name), "r", encoding="utf-8") as handle:
                source = handle.read()
            payload, suffix = degrade_corpus_file(source, degrade)
            upload_name = name if suffix == ".md" else f"{name}{suffix}"
            document = await knowledge.upload_document(
                session, f"{upload_name}#{fingerprint}", payload,
                eval_user.workspace_id, uploader_id=EVAL_USER_ID,
            )
            total_chunks += document.chunks
            # 入库自检把文档判成 failed 时必须说出来。不说的话表现是召回率
            # 集体归零,看起来像检索坏了——而实际上是语料根本没进库,
            # 这恰恰是 dirty-* 变体**预期**的结果之一(scanned 就该全军覆没)。
            if document.status != "indexed":
                rejected.append(f"{upload_name}({document.status})")
        if rejected:
            logger.warning(
                "[degrade=%s] 入库自检拒收 %s/%s 篇: %s",
                degrade,
                len(rejected),
                len(files),
                ", ".join(rejected),
            )
        return total_chunks, True
    finally:
        session.close()


def _eval_workspace_id(session: Any) -> str:
    user = session.query(User).filter(User.id == EVAL_USER_ID).first()
    return user.workspace_id if user and user.workspace_id else EVAL_USER_ID


def _document_label(name: str) -> str:
    """去掉指纹与降级后缀，还原成金标准里标注的文件名。

    降级会改后缀（``hr-handbook.md`` → ``hr-handbook.md.txt``），因为
    ``chunking._looks_like_markdown`` 会看扩展名——不改的话"PDF 没有标题层级"
    这个损伤会被扩展名兜回来，测出来的差值是假的。而金标准按原始文件名标注，
    所以这里必须把后缀摘掉，否则**所有** ``expected_documents`` 都对不上，
    召回率集体归零，看起来像检索坏了。
    """
    base = name.split("#", 1)[0]
    # 后缀表必须和 degrade_corpus_file 的返回值保持一致。漏一个的症状是
    # **那个降级模式的召回集体归零**，看起来像检索坏了——而实际上只是标签没剥掉。
    # test_corpus_degrade 里有一条断言按 DEGRADATIONS 全量核对这张表。
    for suffix in (".txt", ".pdf", ".docx", ".xlsx"):
        if base.endswith(suffix) and base.count(".") > 1:
            return base[: -len(suffix)]
    return base


def _degraded_stages(trace: Any) -> list[str]:
    """这一题里哪些检索增强阶段降级了。

    ``services/retriever._mark_degraded`` 往当前 span 写 ``degraded_stage``，
    这里把整棵 trace 扫一遍收集起来。之所以要汇总进报告：降级只写日志的话，
    报告上呈现的是"这个技术没有增益"，与"这个技术根本没跑"**长得一模一样**。
    ``rerank-api`` 就是这样一直被读成前者的（端点返 429/1113）。

    返回列表而不是集合：同一题里同一阶段可能降级多次（多查询下每路一次），
    次数本身是信息——偶发失败和 100% 失效是两回事。
    """
    if trace is None:
        return []
    return [
        stage
        for span in trace.spans
        if (stage := span.attributes.get("degraded_stage"))
    ]


def _degraded_reasons(trace: Any) -> list[str]:
    """这一题里每次降级的原因，形如 ``rerank:truncated``。

    单独一个函数而不是把原因塞进 ``_degraded_stages``：那个函数的返回值被
    ``degradedStages`` 按阶段计数用，混进原因会让"同一阶段不同原因"变成两个阶段。

    为什么原因必须进报告：**修法完全取决于原因。** ``truncated`` 要加
    ``RAG_RERANK_MAX_TOKENS``、``no_json`` 要换提示词或模型、``http_429_1113``
    是额度没开通得换端点。一个没有原因的次数（"rerank 降级 10 次"）只能靠猜，
    或者去翻日志——而这个项目反复踩的正是"结论在报告里、细节在日志里"这个割裂。
    """
    if trace is None:
        return []
    reasons: list[str] = []
    for span in trace.spans:
        stage = span.attributes.get("degraded_stage")
        if not stage:
            continue
        reason = span.attributes.get("degraded_reason")
        # 没有原因的降级也要计数,只是归到 unknown:少算一次会让
        # degradedCases 和原因数对不上,而对不上比缺信息更难查
        reasons.append(f"{stage}:{reason or 'unknown'}")
    return reasons


def _span_totals(
    trace: Any,
) -> tuple[int, int, float | None, str | None, set[str]]:
    """把一棵 trace 的 token 与成本加总。成本按币种分别累加，混币时不合并。

    第五个返回值是**算不出价的模型名集合**。价目表命中不了时 ``estimate_cost``
    返回 ``None``，这是有意的("宁可承认不知道")，但光是跳过它就让成本列可以
    静默变空:2026-08-23 查出 ``model_prices.json`` 从来没建过,于是历史上**所有**
    报告的 ``cost`` 都是 ``None``,而 25 个变体的结论全是单边的——只有准确度,
    没有代价。换个模型名、改个渠道都会重演一次。

    所以把"谁没算出价"一路带到 summary 里。成本列为空时报告本身就能说出原因,
    而不是让读的人以为这套 eval 不测成本。
    """
    if trace is None:
        return 0, 0, None, None, set()
    prompt = sum(span.prompt_tokens or 0 for span in trace.spans)
    completion = sum(span.completion_tokens or 0 for span in trace.spans)

    by_currency: dict[str, float] = {}
    unpriced: set[str] = set()
    for span in trace.spans:
        # 没有 token 的 span 不算漏价:检索、工具执行这些本来就没有 token
        if not (span.prompt_tokens or span.completion_tokens):
            continue
        cost = estimate_cost(
            span.model, span.prompt_tokens, span.completion_tokens, span.cached_tokens
        )
        if cost is None:
            unpriced.add(span.model or "<unknown>")
            continue
        by_currency[cost.currency] = by_currency.get(cost.currency, 0.0) + float(
            cost.amount
        )
    if not by_currency:
        return prompt, completion, None, None, unpriced
    # 单币种是常态；真出现多币种就只报最大的那个并在报告里注明局限
    currency = max(by_currency, key=lambda key: by_currency[key])
    return prompt, completion, by_currency[currency], currency, unpriced


async def _run_case(
    knowledge: KnowledgeService,
    judge: AnswerJudge,
    adapter: Any,
    case: QuestionCase,
    top_k: int,
    answer_prompt: prompt_library.PromptTemplate,
    answer_temperature: float = 0.0,
) -> QuestionResult:
    session = SessionLocal()
    try:
        async with tracer.trace(user_id=EVAL_USER_ID, chat_id=f"eval:{case.id}") as trace:
            started = time.perf_counter()
            retrieval_started = time.perf_counter()
            context, citations = await knowledge.build_rag_context_with_citations(
                session, case.question, _eval_workspace_id(session), top_k=top_k
            )
            retrieval_ms = int((time.perf_counter() - retrieval_started) * 1000)

            ranked = [_document_label(item["document_name"]) for item in citations]
            relevant = set(case.expected_documents)
            retrieval_scores = metrics.evaluate_ranking(ranked, relevant, k=top_k)

            answer = ""
            try:
                completion = await _RATE_GATE.call(
                    lambda: adapter.complete(
                        messages=[
                            {
                                "role": "user",
                                "content": answer_prompt.render(
                                    context=context or "(无参考内容)",
                                    question=case.question,
                                ),
                            }
                        ],
                        tools=[],
                        model=settings.LLM_MODEL,
                        temperature=answer_temperature,
                        max_tokens=_ANSWER_MAX_TOKENS,
                        purpose="eval_answer",
                    )
                )
                answer = completion.content or ""
            except Exception as exc:
                logger.warning(
                    "answer generation failed for %s: %s", case.id, type(exc).__name__
                )

            verdict = await _RATE_GATE.call(
                lambda: judge.judge(
                    question=case.question,
                    answer=answer,
                    context=context,
                    answerable=case.answerable,
                )
            )
            latency_ms = int((time.perf_counter() - started) * 1000)

        prompt_tokens, completion_tokens, cost, currency, unpriced = _span_totals(trace)
        degraded = _degraded_stages(trace)
        degraded_reasons = _degraded_reasons(trace)
        return QuestionResult(
            case=case,
            retrieved_documents=ranked,
            retrieval=retrieval_scores,
            answer=answer,
            keyword_coverage=metrics.keyword_coverage(answer, case.must_include),
            avoid_hits=sum(
                1 for phrase in case.must_avoid if phrase.lower() in answer.lower()
            ),
            verdict=verdict,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            currency=currency,
            unpriced_models=unpriced,
            degraded_stages=degraded,
            degraded_reasons=degraded_reasons,
            latency_ms=latency_ms,
            retrieval_ms=retrieval_ms,
        )
    finally:
        session.close()


def summarize(variant: Variant, results: list[QuestionResult]) -> dict[str, Any]:
    """把逐题结果汇总成一行。裁判失败的样本从均值里剔除而不是当 0 分。"""
    answerable = [r for r in results if r.case.answerable]
    absent = [r for r in results if not r.case.answerable]
    top_k = int(variant.overrides.get("RAG_TOP_K", settings.RAG_TOP_K))

    graded = [r for r in answerable if not r.verdict.failed]
    abstention_graded = [r for r in absent if not r.verdict.failed]
    # 没有来源标注的样本(比如从线上反馈导出的回归用例)不能进检索均值:
    # recall_at_k 对空的相关集返回 1.0,混进来会把召回率凭空拉高。
    ranked_cases = [r for r in answerable if r.case.expected_documents]

    summary: dict[str, Any] = {
        "variant": variant.name,
        "description": variant.description,
        "questions": len(results),
        "retrievalScored": len(ranked_cases),
        f"recall@{top_k}": metrics.mean(
            [r.retrieval[f"recall@{top_k}"] for r in ranked_cases]
        ),
        # precision 每条 case 一直在算(metrics.py:87),但从没汇总过——又是"记录了
        # 没冒泡"。它现在是这套 eval 里**唯一还没饱和的检索指标**:2026-08-25 实测
        # baseline recall@5 = 1.0000 而 precision@5 = 0.3852。recall 到顶之后
        # "混合召回 vs 纯稠密""重排开 vs 关"在报告上长得一样,不是没差别,
        # 是那把尺子量不出差别;precision 还有 0.6 的量程。
        #
        # 它量的是"top_k 里有多少是真该在的"。重排的作用恰好是把噪声挤出前 k,
        # 所以这一列才是重排类变体该看的主指标。
        f"precision@{top_k}": metrics.mean(
            [r.retrieval[f"precision@{top_k}"] for r in ranked_cases]
        ),
        f"ndcg@{top_k}": metrics.mean(
            [r.retrieval[f"ndcg@{top_k}"] for r in ranked_cases]
        ),
        "mrr": metrics.mean([r.retrieval["mrr"] for r in ranked_cases]),
        "keywordCoverage": metrics.mean([r.keyword_coverage for r in answerable]),
        "faithfulness": metrics.mean(
            [r.verdict.faithfulness for r in graded if r.verdict.faithfulness is not None]
        ),
        "relevance": metrics.mean(
            [r.verdict.relevance for r in graded if r.verdict.relevance is not None]
        ),
        "abstentionRate": metrics.mean(
            [1.0 if r.verdict.abstained else 0.0 for r in abstention_graded]
        ),
        # 拒答率的分母。变体之间可以不同(裁判在某个变体上多失败一次),而
        # 1.000 vs 0.667 里那个 0.667 是 6/9 不是 6.67/10——不写出来就没人知道
        # 两列的分母其实不一样
        "abstentionGraded": len(abstention_graded),
        # 裁判自己的理由与 abstained 矛盾的条数。见 structured.AbstentionVerdict
        "judgeInconsistent": sum(1 for r in results if r.verdict.inconsistent),
        "judgeFailures": sum(1 for r in results if r.verdict.failed),
        "promptTokens": sum(r.prompt_tokens for r in results),
        "completionTokens": sum(r.completion_tokens for r in results),
        "avgLatencyMs": metrics.mean([float(r.latency_ms) for r in results]),
        "avgRetrievalMs": metrics.mean([float(r.retrieval_ms) for r in results]),
    }

    # 分位延迟，理由同 agent_runner：均值掩盖长尾，P95 才是分维度指标
    _latencies = [float(r.latency_ms) for r in results]
    summary["p50LatencyMs"] = metrics.percentile(_latencies, 50)
    summary["p95LatencyMs"] = metrics.percentile(_latencies, 95)

    costs = [r.cost for r in results if r.cost is not None]
    summary["cost"] = sum(costs) if costs else None
    summary["currency"] = next((r.currency for r in results if r.currency), None)
    # 算不出价的模型。非空就说明成本列不完整——此时 cost 是**下界**而不是总额,
    # 拿它去比"哪个变体更划算"会偏向漏价多的那个。空列表才允许直接比。
    unpriced = sorted({name for r in results for name in r.unpriced_models})
    summary["unpricedModels"] = unpriced or None

    # 降级次数按阶段分开计。这一项的作用是让"配了但没生效"在**报告里**就读不通：
    # 一个变体带着非零降级数还宣称与 baseline 相同，那结论是"它没跑"，
    # 不是"它没用"。两者的处置完全相反——前者去修配置，后者去掉这个技术。
    stage_counts: dict[str, int] = {}
    for r in results:
        for stage in r.degraded_stages:
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
    summary["degradedStages"] = (
        dict(sorted(stage_counts.items())) if stage_counts else None
    )
    # **不要写成 ``or None``。** 那样"一次都没降级"和"根本没统计过"会序列化成
    # 同一个值，而报告把它渲染成 `0/54`——读起来是"测过了，很干净"。
    # 这一列存在的全部意义就是分开这两件事：`0` 去信任它的指标，
    # "没统计过"先去修埋点。2026-08-27 那份报告里 rerank-api 明明有重排环节，
    # 这里却和无增强的 baseline 一样是 None。
    summary["degradedCases"] = sum(1 for r in results if r.degraded_stages)
    # 原因分布。次数说明"生效了没有",原因说明"该改哪里"——只有次数的话
    # 下一步只能靠猜(rerank 那 10 次是预算不够还是模型不输出 JSON?)
    reason_counts: dict[str, int] = {}
    for r in results:
        for reason in r.degraded_reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    summary["degradedReasons"] = (
        dict(sorted(reason_counts.items())) if reason_counts else None
    )

    # 按探针类型拆开看：BM25 应该主要提升 lexical，重排主要提升 ndcg
    by_probe: dict[str, list[float]] = {}
    for result in ranked_cases:
        by_probe.setdefault(result.case.probe, []).append(
            result.retrieval[f"recall@{top_k}"]
        )
    summary["recallByProbe"] = {
        probe: metrics.mean(values) for probe, values in sorted(by_probe.items())
    }

    # 注入抵抗率单独一档。这个数字是"当前提示词 + 护栏"的联合表现,
    # 不能只归因于任何一方。
    #
    # 判据是 ``probe == "injection"``,**不是**"有没有标 must_avoid"。
    # must_avoid 是个通用的"不该出现的字符串"机制:2026-08-25 加的 absent 硬负例
    # 拿它抓编造的数字(问丧假几天,回答里不该出现"3 天"),那些样本混进分母会把
    # 注入抗性算高——一个从不被注入带走、但会编数字的系统会拿到虚高的分。
    # agent_runner 早先踩过同一个坑并改成按 probe 判,这里跟上。
    # 非注入样本的 must_avoid 命中另计在 fabricationRate 里。
    injection = [r for r in results if r.case.probe == "injection"]
    summary["injectionCases"] = len(injection)
    summary["injectionResistRate"] = (
        metrics.mean([1.0 if r.avoid_hits == 0 else 0.0 for r in injection])
        if injection
        else None
    )

    # 编造率:非注入样本里 must_avoid 命中的比例。
    #
    # 这一项冲着"拒答率高但拒得不干净"那种情况:模型说了"资料里没写",紧接着
    # 又补一句"一般是 3 天"。拒答率判的是**有没有承认不知道**,而这里判的是
    # **有没有在承认之后接着编**。两者可以同时为高,那正是最难查的一种失败——
    # 读起来像个诚实的回答,里面却带着一个凭空的数字。
    fabrication = [r for r in results if r.case.must_avoid and r.case.probe != "injection"]
    summary["fabricationCases"] = len(fabrication)
    summary["fabricationRate"] = (
        metrics.mean([1.0 if r.avoid_hits else 0.0 for r in fabrication])
        if fabrication
        else None
    )
    return summary


async def run_variant(
    variant: Variant, cases: list[QuestionCase], answer_temperature: float = 0.0
) -> tuple[dict[str, Any], list[QuestionResult]]:
    """套用变体配置跑完一轮，结束后恢复原配置。"""
    original = {key: getattr(settings, key) for key in variant.overrides}
    for key, value in variant.overrides.items():
        setattr(settings, key, value)

    try:
        adapter = OpenAICompatibleAdapter()
        knowledge = KnowledgeService()
        judge = AnswerJudge(adapter)

        # 变体可能改了 PROMPT_EVAL_ANSWER_VERSION，所以在套用配置之后才解析模板
        answer_prompt = prompt_library.get(ANSWER_PROMPT_KEY)

        chunks, reindexed = await ensure_corpus(knowledge)
        logger.info(
            "[%s] corpus ready: %s chunks%s | prompt=%s",
            variant.name,
            chunks,
            " (reindexed)" if reindexed else "",
            answer_prompt.ref,
        )

        top_k = int(variant.overrides.get("RAG_TOP_K", settings.RAG_TOP_K))
        results: list[QuestionResult] = []
        for index, case in enumerate(cases, start=1):
            result = await _run_case(
                knowledge, judge, adapter, case, top_k, answer_prompt,
                answer_temperature=answer_temperature,
            )
            results.append(result)
            logger.info(
                "[%s] %s/%s %s recall=%.2f",
                variant.name,
                index,
                len(cases),
                case.id,
                result.retrieval[f"recall@{top_k}"],
            )
        summary = summarize(variant, results)
        summary["corpusChunks"] = chunks
        summary["answerPrompt"] = answer_prompt.version
        return summary, results
    finally:
        for key, value in original.items():
            setattr(settings, key, value)


async def run(
    variants: list[Variant], cases: list[QuestionCase], answer_temperature: float = 0.0
) -> dict[str, Any]:
    # 评估依赖埋点来算成本与延迟，强制打开
    settings.TELEMETRY_ENABLED = True

    summaries: list[dict[str, Any]] = []
    details: dict[str, list[dict[str, Any]]] = {}
    for variant in variants:
        summary, results = await run_variant(variant, cases, answer_temperature)
        summaries.append(summary)
        details[variant.name] = [
            {
                "id": result.case.id,
                "probe": result.case.probe,
                "question": result.case.question,
                "expected": result.case.expected_documents,
                "retrieved": result.retrieved_documents,
                "retrieval": result.retrieval,
                "keywordCoverage": result.keyword_coverage,
                "avoidHits": result.avoid_hits,
                "faithfulness": result.verdict.faithfulness,
                "relevance": result.verdict.relevance,
                "abstained": result.verdict.abstained,
                "judgeInconsistent": result.verdict.inconsistent,
                "judgeReason": result.verdict.reason,
                "judgeFailed": result.verdict.failed,
                "answer": result.answer,
                "latencyMs": result.latency_ms,
                # 逐题的降级原因。summary 里已经有按原因的计数,但计数说不出
                # "是哪几道题",而修的时候要看的正是那几道题的问题长什么样。
                # 2026-08-28 那份报告说 rerank:invalid x3,想知道是哪 3 道只能
                # 重跑一次——这就是"记录了但没冒泡到能用的那一层"。
                # 空列表省掉,不给 51 道正常题各加一个 [] 把文件撑大。
                "degradedReasons": result.degraded_reasons or None,
            }
            for result in results
        ]
    return {"summaries": summaries, "details": details}

