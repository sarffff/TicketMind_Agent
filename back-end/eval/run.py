"""评估 CLI。

    python -m eval.run                                  # 只跑 baseline
    python -m eval.run --variants all                   # 全部变体对照
    python -m eval.run --variants baseline,dense-only    # 指定对照
    python -m eval.run --limit 5                         # 先小样本试跑

会真实调用模型与 embedding 接口，产生费用。--variants all 的开销约等于
问题数 x 变体数 次生成 + 同样次数的裁判调用，先用 --limit 估一下再放开。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any

from eval import metrics, runner
from eval.variants import VARIANTS, resolve
from services.console import utf8_output
from services.clock import now as app_now

_REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

# 报告表格里展示的列。None 值渲染成 "-"，不要粉饰成 0
_COLUMNS = [
    ("variant", "变体"),
    ("answerPrompt", "提示词"),
    ("recall", "recall@k"),
    # recall 已经饱和(k=5 时恒为 1.0000),precision 没有。放在它右边是为了让
    # "召回到顶但一半以上是噪声"这件事在同一眼里读到。
    ("precision", "precision@k"),
    ("ndcg", "nDCG@k"),
    ("mrr", "MRR"),
    ("faithfulness", "忠实度"),
    ("relevance", "相关性"),
    ("abstentionRate", "拒答率"),
    # 紧挨着拒答率,因为它俩必须一起读:拒答率高而编造率非零,是"说了不知道之后
    # 又补一个数字"——读起来像诚实回答,里面带着凭空的值,比直接答错更难查。
    ("fabricationRate", "编造率"),
    ("injectionResistRate", "抗注入率"),
    # 降级列。放在指标**右边紧邻**的位置,因为它是读那几个指标的前提条件:
    # 一个变体带着 46/46 降级还宣称与 baseline 相同,结论是"它没跑",不是"它没用"。
    # 这两件事的处置完全相反(去修配置 vs 去掉这个技术),而在这一列出现之前,
    # 报告里它们长得一模一样——rerank-api 因为端点返 429 从未真正执行过,
    # 报告上和 baseline 逐位相同,持续了很久没人发现。
    ("degradedCases", "降级"),
    ("promptTokens", "输入 token"),
    ("completionTokens", "输出 token"),
    ("cost", "成本"),
    ("avgLatencyMs", "平均耗时 ms"),
    ("p95LatencyMs", "P95 耗时 ms"),
]


def _pick(summary: dict[str, Any], key: str) -> Any:
    """recall/precision/ndcg 的键带 top_k 后缀，这里按前缀取值。"""
    if key in ("recall", "precision", "ndcg"):
        match = next((k for k in summary if k.startswith(f"{key}@")), None)
        return summary.get(match) if match else None
    return summary.get(key)


def _degraded_cell(summary: dict[str, Any]) -> str:
    """降级列渲染成 ``N/总数``,而不是一个裸计数。

    分母是必须的:``12`` 说不清是十二分之一还是十二分之十二,而"全部降级"
    和"偶发降级"是两个完全不同的结论。

    没有降级时给 ``0`` 而不是 ``-``:这一列的 ``-`` 会被读成"没这个数据",
    而实际含义是"一次都没降级",那是个**好消息**,不该长得像缺数据。
    ``runner`` 那边用 ``or None`` 是为了让 JSON 干净,所以这里补回 0。
    """
    total = summary.get("questions")
    count = summary.get("degradedCases") or 0
    if not total:
        return _format(summary.get("degradedCases"))
    return f"{count}/{total}"


def _format(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 100 else f"{value:.0f}"
    return str(value)


def _degraded_case_ids(
    details: dict[str, list[dict[str, Any]]] | None,
    variant: str | None,
    *,
    limit: int = 8,
) -> list[str]:
    """这个变体里降级了的题号，按报告顺序，最多 ``limit`` 个。

    ``details`` 允许缺失:2026-08-28 之前的报告逐题不带 ``degradedReasons``,
    此时返回空列表让渲染器少写一行,而不是报错——历史报告要还能重渲染。

    截断而不是全列:全量降级时这里会是 54 个题号,那一行会把整节冲掉,
    而"全量降级"本身已经有专门的提示。超出的部分给个数量。
    """
    rows = (details or {}).get(variant or "") or []
    named = [
        str(row.get("id"))
        for row in rows
        if row.get("degradedReasons") or row.get("degraded_reasons")
    ]
    if len(named) > limit:
        return named[:limit] + [f"…另有 {len(named) - limit} 条"]
    return named


def _render_degradation(
    summaries: list[dict[str, Any]],
    details: dict[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    """降级明细与漏价模型。全都干净时**整节不出现**。

    这一节是主表那个计数的展开:哪个阶段降的、降了几次。阶段名很重要——
    ``rerank`` 降级和 ``hyde`` 降级要修的东西完全不同(一个是重排端点/额度,
    一个是辅助生成的 token 预算)。

    "全都干净时不出现"是有意的:这个项目里最贵的一类错误是把警告混在常态噪音里,
    于是没人再读它。这一节只在**真的有问题**时出现,出现就值得停下来看。

    漏价模型放在同一节,因为它是同一个形状的问题:数字看起来正常,但它是个下界。
    拿一个漏价的成本去比"哪个变体更划算"会系统性偏向漏得多的那个。
    """
    flagged = [
        s for s in summaries if s.get("degradedStages") or s.get("unpricedModels")
    ]
    if not flagged:
        return []

    lines = ["", "## ⚠ 降级与计价缺口", ""]
    # 按变体分组,一个变体一个条目。两个 for 循环各自输出会让同时中两条的变体
    # 出现两次同名粗体,读起来像渲染坏了——而这一节的全部作用就是被人认真读。
    for summary in flagged:
        lines.append(f"- **{summary['variant']}**")
        stages = summary.get("degradedStages")
        if stages:
            detail = "、".join(f"{stage} x{count}" for stage, count in stages.items())
            lines.append(f"  - 降级：{detail}")
            # 原因紧跟在次数后面。次数回答"生效了没有",原因回答"该改哪里"——
            # 只有次数时下一步只能靠猜或者去翻日志。
            reasons = summary.get("degradedReasons")
            if reasons:
                lines.append(
                    "  - 原因："
                    + "、".join(f"{name} x{count}" for name, count in reasons.items())
                )
            # 再往下一层:是哪几道题。原因回答"该改哪里",题号回答"拿哪道题去复现"。
            # 2026-08-28 报告写着 rerank:invalid x3,想知道是哪 3 道只能整轮重跑——
            # 计数和原因都在报告里了,唯一缺的是这一行。
            named = _degraded_case_ids(details, summary.get("variant"))
            if named:
                lines.append(f"  - 涉及问题：{'、'.join(named)}")
            # 全量降级单独点出来。这是"配了等于没配"最典型的样子,而它在指标上
            # 的表现是**和 baseline 完全一致**——最容易被读成"这个技术没有增益"。
            total = summary.get("questions") or 0
            if total and any(count >= total for count in stages.values()):
                lines.append(
                    f"    - 有阶段在全部 {total} 个问题上都降级了，"
                    "该变体这一环**没有真正执行**。此时它的指标等于 baseline 是"
                    "预期结果，不能据此判断这个技术有没有用——先去修配置。"
                )
        if summary.get("unpricedModels"):
            models = "、".join(summary["unpricedModels"])
            lines.append(
                f"  - 计价缺口：{models} 没有价目，该行成本是**下界**而非总额，"
                "不能与其他变体直接比较。"
            )
    return lines


def _composition(summary: dict[str, Any]) -> str:
    """表头的问题数必须带拆分,因为**各列的分母不是同一个**。

    检索四列(recall/precision/nDCG/MRR)只在标了 expected_documents 的样本上平均;
    拒答率只在 absent 样本上平均。2026-08-25 加了 8 条硬负例之后这个差距变成
    54 总数 / 44 计检索,单写"问题数：54"会让人以为 recall 是 54 条的均值。

    差距小的时候这个问题也在,只是不显眼(老报告是 46/44);加硬负例是把它放大到
    读者一定会误读的程度——所以这一行不是补充信息,是修一处已经存在的误导。
    """
    total = summary.get("questions") or 0
    scored = summary.get("retrievalScored")
    if scored is None or scored == total:
        return f"问题数：{total}"
    return f"问题数：{total}（检索计分 {scored}，其余为拒答类无来源标注）"


def _corpus_line(summaries: list[dict[str, Any]]) -> list[str]:
    """语料分块数。它是"这轮测的哪版语料"的指纹,不是背景装饰。

    没有它,两份分块数不同的报告(扩容前 40、扩容后 92)看起来完全可比,而实际上
    指标之间没有可比性。项目里已经因为 ``ensure_corpus`` 早退而静默测过一次旧
    索引,那次的报告长得和正常报告一模一样。

    各变体分块数不同时逐个列出:那意味着变体改了分块配置,此时检索指标的差异
    有一部分来自"索引都不是同一个",要先知道这件事再读下面的表。
    """
    counts = {s["variant"]: s.get("corpusChunks") for s in summaries}
    present = {v: c for v, c in counts.items() if c is not None}
    if not present:
        return []
    unique = set(present.values())
    if len(unique) == 1 and len(present) == len(counts):
        return [f"语料分块：{unique.pop()}"]
    detail = "、".join(f"{v} {c if c is not None else '未知'}" for v, c in counts.items())
    return [f"语料分块（各变体不同，检索差异有一部分来自索引不同）：{detail}"]


def _paired(
    details: dict[str, list[dict[str, Any]]], left: str, right: str, key: str
) -> tuple[list[float], list[float]]:
    """按 **case id** 对齐两个变体的逐题指标。

    不能按列表下标配对:``ranked_cases`` 在每个变体里各自过滤(没有
    ``expected_documents`` 的题不进检索均值),两侧的下标会错位。错位之后配对
    检验照样会算出一个漂亮的区间,只是那个区间毫无意义——**这类 bug 不会报错**,
    所以这里按 id 取交集。
    """
    def index(variant: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for case in details.get(variant) or []:
            retrieval = case.get("retrieval") or {}
            value = retrieval.get(key)
            # 没标来源的题(线上反馈导出的回归用例)不计:recall 对空相关集返回 1.0
            if value is not None and case.get("expected"):
                out[str(case.get("id"))] = float(value)
        return out

    a, b = index(left), index(right)
    ids = sorted(set(a) & set(b))
    return [a[i] for i in ids], [b[i] for i in ids]


def _render_significance(report: dict[str, Any]) -> list[str]:
    """各变体相对 baseline 的差值 + 95% 置信区间。

    主表给点估计,这一节给不确定性。少了它,``0.974`` vs ``0.971`` 会被读成
    "略微变差",而实测那 0.003 全部来自 44 条里的 1 条。
    """
    summaries = report.get("summaries") or []
    details = report.get("details") or {}
    if len(summaries) < 2 or not details:
        return []

    names = [s["variant"] for s in summaries]
    base = "baseline" if "baseline" in names else names[0]
    # 指标名从 **details** 里取,不从 summary。summary 是 2026-08-25 才有
    # precision@k 的,而 details 里每条 case 一直都有——从 details 取,这一节
    # 就能跑在**历史报告**上,让过去那些花过钱的运行重新变得可分析。
    #
    # precision 和 nDCG 都要:README 里让重排类变体看这两个,那这一节就得都覆盖。
    # 两者分得开的东西不一样——precision 看"前 k 条里有多少该在",nDCG 看顺序。
    # 8/24 那份报告里 precision 逐题全同,而 nDCG 恰好有 1 条不同。
    available = {
        k
        for case in (details.get(base) or [])
        for k in (case.get("retrieval") or {})
    }
    metric_keys = [
        k
        for k in sorted(available)
        if k.startswith("precision@") or k.startswith("ndcg@")
    ]
    if not metric_keys:
        return []

    rows: list[str] = []
    # (指标, 变体, 现有题数, 需要题数)。只收判定为「分不出来」的——判定已经成立
    # 时再说"要多少题"是废话。
    needed: list[tuple[str, str, int, int]] = []
    for metric_key in metric_keys:
        for name in names:
            if name == base:
                continue
            left, right = _paired(details, base, name, metric_key)
            stats = metrics.paired_bootstrap(left, right)
            if not stats:
                continue
            verdict = "**真实差异**" if stats["significant"] else "分不出来"
            # 胜负拆开印。均值 +0.0564 既可能是"18 好 10 差"也可能是"28 条各涨
            # 一点点",而这两件事上线后的可预测性完全不同——判定那一列说不出区别。
            record = (
                f"{stats['wins']} 好 / {stats['losses']} 差"
                if stats["changed"]
                else "全部持平"
            )
            stability = metrics.verdict_stability(left, right)
            # 1.0 和 0.0 都是"结论不靠随机数",中间值才是警告
            shaky = stability is not None and 0.0 < stability < 1.0
            stability_cell = (
                "—"
                if stability is None
                else (f"**{stability:.0%}** ⚠" if shaky else f"{stability:.0%}")
            )
            rows.append(
                f"| {metric_key} | {name} | {stats['observed']:+.4f} | "
                f"[{stats['low']:+.4f}, {stats['high']:+.4f}] | "
                f"{stats['n']} | {record} | {stability_cell} | {verdict} |"
            )
            if not stats["significant"]:
                wanted = metrics.required_sample_size(left, right)
                if wanted and wanted > stats["n"]:
                    needed.append((metric_key, name, stats["n"], wanted))

    if not rows:
        return []

    lines = [
        "",
        f"## 相对 {base} 的显著性",
        "",
        # 「种子稳定」这一列取代了上一版的「可分辨阈值」。后者用正态近似算,
        # 会和自助法判定互相矛盾(rerank 的 precision 差值小于阈值却判显著),
        # 而且我把它当成全局常数印在脚注里,实际印出来的是循环里最后一个变体的
        # 那个数——又一次"算了但没冒泡对"。
        "| 指标 | 变体 | 差值 | 95% 置信区间 | 配对题数 | 逐题胜负 | 种子稳定 | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        *rows,
        "",
        "- **区间跨过 0 就是「这份数据分不出来」**，既不是「两者相同」，也不是",
        "  「这个技术没用」。可能是差值小于噪声，也可能是题不够多。",
        "- 配对自助法：逐题相减，所以「这道题本身难不难」被消掉了，功效比各自算",
        "  均值高得多。用自助法而不是 t 检验，因为 precision@k 取值离散、n 只有",
        "  四十几条，正态性假设站不住。",
    ]
    lines += [
        "- **种子稳定 = 换 20 个随机种子重跑，判定还成立的比例。** 100% 和 0% 都是",
        "  好消息（结论不来自随机数）；**中间值带 ⚠，那才是问题**——它意味着这条",
        "  结论有一定概率反过来，此时该加题，而不是挑一个好看的种子。",
        "- **差值更大不等于更可信。** 差值大但判不出来，说明它「有时帮很多、有时",
        "  帮倒忙」——逐题方差大。这种变体上线后的表现最难预测。",
        "  差值判不出来时先看降级列：「配了没生效」和「生效了但没增益」长得一样。",
        "- **逐题胜负是均值的必要补充。** 「18 好 / 10 差、均值 +0.0564」和「28 条",
        "  各涨一点点、均值 +0.0564」在差值那一列一模一样，但前者上线后的表现无法",
        "  预测，后者是稳定收益。判定列只会对两者都说「分不出来」。",
    ]
    if needed:
        lines += [
            "",
            "### 「分不出来」的那些，需要多少题",
            "",
            "| 指标 | 变体 | 现有 | 约需 |",
            "| --- | --- | --- | --- |",
            *(
                f"| {metric} | {name} | {have} | **{want}** |"
                for metric, name, have, want in needed
            ),
            "",
            "- 这一节把「不确定」换成一个**可执行的数**：按当前效应量与逐题方差，",
            "  要 80% 把握检出这个差值需要多少条配对题。远大于现有题数 = 结论是",
            "  「题不够」；接近现有题数 = 真的没有稳定效应。",
            "- 只是量级估计。它假设新题与现有题同分布，而扩容常常改变分布——",
            "  上一次语料 6→13 篇就没解开 recall 饱和。",
            "- **别据此盲目扩题。** 先看上面的胜负拆分：如果是「有时帮很多、有时帮",
            "  倒忙」，加题只会把两边一起加大，均值照样在 0 附近。那种情况该做的是",
            "  找出帮倒忙的那一类并单独处理。",
        ]
    lines += _render_precision_decomposition(report)
    return lines


def _render_precision_decomposition(report: dict[str, Any]) -> list[str]:
    """把 precision 的变化拆成「真的变了」和「只是分块数变了」。

    这一节要 ``details`` 里的 ``retrieved``——summary 里没有文档集合，拆不出来。

    只在**真的有假象**时出现。全是真实变化时这一节是噪音，而这个项目里最贵的
    一类错误就是把警告混在常态噪音里，于是没人再读它。
    """
    summaries = report.get("summaries") or []
    details = report.get("details") or {}
    if len(summaries) < 2 or not details:
        return []
    names = [s["variant"] for s in summaries]
    base = "baseline" if "baseline" in names else names[0]

    rows: list[str] = []
    for name in names:
        if name == base:
            continue
        left = {
            str(c.get("id")): c
            for c in details.get(base) or []
            if c.get("expected") and (c.get("retrieval") or {})
        }
        right = {
            str(c.get("id")): c
            for c in details.get(name) or []
            if c.get("expected") and (c.get("retrieval") or {})
        }
        pairs = []
        for case_id in sorted(set(left) & set(right)):
            lc, rc = left[case_id], right[case_id]
            key = next(
                (k for k in lc["retrieval"] if k.startswith("precision@")), None
            )
            if key is None or rc["retrieval"].get(key) is None:
                continue
            # 缺 retrieved 的题直接跳过。两侧都缺时 set([]) == set([]) 成立，
            # 会被判成"文档集合没变"→ 记为假象，而它其实是**数据缺失**。
            # 把未知算成假象会让老报告(没有这个字段)凭空长出一整节警告。
            if not lc.get("retrieved") or not rc.get("retrieved"):
                continue
            pairs.append(
                (
                    list(lc.get("retrieved") or []),
                    list(rc.get("retrieved") or []),
                    list(lc.get("expected") or []),
                    rc["retrieval"][key] - lc["retrieval"][key],
                )
            )
        if not pairs:
            continue
        parts = metrics.decompose_precision_change(pairs)
        if not parts["artifactCases"]:
            continue
        rows.append(
            f"| {name} | {parts['realImproved']} 好 / {parts['realRegressed']} 差 | "
            f"{parts['realMean']:+.4f} | {parts['artifactCases']} | "
            f"{parts['artifactMean']:+.4f} |"
        )

    if not rows:
        return []
    return [
        "",
        "## precision 变化的成分",
        "",
        "| 变体 | 文档集合真的变了 | 真实贡献 | 只是分块数变了 | 假象贡献 |",
        "| --- | --- | --- | --- | --- |",
        *rows,
        "",
        "- `precision@k` 按**文档**去重、按**位置**计数，所以两侧取回的文档集合完全",
        "  相同时，光是分块数不同就能让它变动——那不是检索变好或变差。",
        "- 2026-08-27 最难看的一条 `postmortem-p2-trigger` 是 **-0.500**，而",
        "  **两侧都只取回了正确文档**（1 个分块 → 2 个分块），nDCG 两边都是 1.000。",
        "  这个指标在惩罚「把对的文档多取回来一点」。不拆开的话，读者要么被这一条",
        "  带偏，要么无从判断整体差值有多少可信。",
        "- 假象贡献接近 0 时，主表那个差值可以照着读；假象贡献占了大头时，",
        "  该看 nDCG 而不是 precision——前者只看顺序，不受分块数影响。",
    ]


def render_markdown(report: dict[str, Any]) -> str:
    summaries = report["summaries"]
    lines = [
        "# RAG 配置对照报告",
        "",
        f"生成时间：{app_now().isoformat(timespec='seconds')}",
        _composition(summaries[0]) if summaries else "问题数：0",
        *_corpus_line(summaries),
        "",
        "| " + " | ".join(label for _key, label in _COLUMNS) + " |",
        "| " + " | ".join("---" for _ in _COLUMNS) + " |",
    ]
    for summary in summaries:
        cells = [
            _degraded_cell(summary)
            if key == "degradedCases"
            else _format(_pick(summary, key))
            for key, _label in _COLUMNS
        ]
        lines.append("| " + " | ".join(cells) + " |")

    lines += _render_degradation(summaries, report.get("details"))
    # 紧跟主表:点估计刚看完就给不确定性,免得读者已经在心里下了结论
    lines += _render_significance(report)

    lines += ["", "## 按探针类型的召回", ""]
    probes = sorted({p for s in summaries for p in s.get("recallByProbe", {})})
    if probes:
        lines.append("| 变体 | " + " | ".join(probes) + " |")
        lines.append("| " + " | ".join("---" for _ in range(len(probes) + 1)) + " |")
        for summary in summaries:
            by_probe = summary.get("recallByProbe", {})
            cells = [_format(by_probe.get(probe)) for probe in probes]
            lines.append(f"| {summary['variant']} | " + " | ".join(cells) + " |")

    # 裁判自我矛盾:abstained=false 而理由写着"正确拒答"。这个必须显式冒泡,
    # 因为它污染的是拒答率——而拒答率的量程是 precision 的十倍,是这份报告里
    # 信号最强的一列。悄悄错掉比没有这一列更糟。
    inconsistent = sum(s.get("judgeInconsistent", 0) for s in summaries)
    if inconsistent:
        lines += [
            "",
            f"> ⚠ 裁判自我矛盾 {inconsistent} 次：`abstained=false` 但理由写着"
            "「正确拒答」。**这一轮的拒答率不可信**，先看 rubric 是否已切到"
            "「reason 在 abstained 之前」的版本。",
        ]
    # 拒答率的分母逐变体列出。1.000 vs 0.667 里的 0.667 可能是 6/9 而不是
    # 6.67/10——分母不同的两列直接比大小是错的
    denominators = {
        s["variant"]: s.get("abstentionGraded")
        for s in summaries
        if s.get("abstentionGraded") is not None
    }
    if len(set(denominators.values())) > 1:
        detail = "、".join(f"{k} {v}" for k, v in denominators.items())
        lines += [
            "",
            f"> ⚠ 拒答率的分母各变体不同（{detail}）：裁判在部分变体上多失败了几次。"
            "这一列**不能直接比大小**，先看分母。",
        ]

    judge_failures = sum(s.get("judgeFailures", 0) for s in summaries)
    if judge_failures:
        lines += [
            "",
            f"> 裁判解析失败 {judge_failures} 次，这些样本已从忠实度/相关性均值中剔除。",
        ]
    lines += [
        "",
        "## 读法",
        "",
        "- **recall@k 已经饱和，别再拿它比变体。** 当前语料 + k=5 下它恒为 1.000，",
        "  所以两个变体在这一列相同不代表检索一样好。有量程的是 precision@k",
        "  （2026-08-25 baseline 实测 0.385）与 nDCG@k：前者量「前 k 条里多少是真该在的」，",
        "  后者量排序。重排类变体应当主要看 precision 与 nDCG。",
        "- **跨报告比较前先对齐表头的语料分块数。** 分块数不同意味着索引不是同一个，",
        "  此时两份报告的检索指标没有可比性（语料从 6 篇扩到 13 篇时分块 40→92）。",
        "- 成本列为空表示没配价目表（见 model_prices.example.json），不代表零成本。",
        "- 忠实度/相关性是 LLM 裁判打分，只用于变体间相对比较，不是绝对质量。",
        "- 探针类型里 `lexical` 考字面精确匹配，`paraphrase` 考语义改写，",
        "  `cross_section` / `cross_document` 考跨段落与跨文档，`absent` 考拒答，",
        "  `injection` 考资料夹带指令时会不会被带走。",
        "- 抗注入率只统计 `probe=injection` 的样本（不是「有没有标 must_avoid」——",
        "  硬负例也用 must_avoid 抓编造的数字，混进分母会把注入抗性算高）。",
        "  它衡量的是「提示词 + 护栏」的联合表现，单独下降不能断定是哪一侧退化，",
        "  要回到 trace 里看 guardrail.* 属性有没有命中。",
        "- **拒答率与编造率必须一起读。** 拒答率判「有没有承认不知道」，编造率判",
        "  「承认之后有没有接着编」。两者可以同时高：模型说「资料里没写」，紧接着",
        "  补一句「一般是 3 天」——那种回答读起来诚实，里面却带着一个凭空的数字，",
        "  比直接答错更难查。编造率只统计带 must_avoid 的非注入样本；`-` 表示",
        "  这一轮没有那类样本，不是零编造。",
        "- 提示词列是本轮用的 eval_rag_answer 版本（正文见 prompts/eval_rag_answer/）。",
        "  只换提示词的变体，检索指标应当与 baseline 逐位相同；不同就说明配置串了。",
        "- **降级列是读其他所有列的前提。** 它统计有多少个问题的检索增强",
        "  （路由 / HyDE / 多查询 / 重排）失败后退回了基础链路。`0/N` 才说明这个",
        "  变体真的按配置跑了；非零时先看上面的降级明细，再决定要不要相信它的指标。",
        "  一个变体带着满额降级还与 baseline 逐位相同，结论是「它没跑」而不是",
        "  「它没用」——前者去修配置，后者才是去掉这个技术。",
        "",
    ]
    return "\n".join(lines)


# 跟温度一起抖的质量/成本指标。recall 在当前语料恒 1.0（不抖），不列。
_VARIANCE_METRICS = [
    ("faithfulness", "忠实度"),
    ("relevance", "相关性"),
    ("abstentionRate", "拒答率"),
    ("fabricationRate", "编造率"),
    ("precision", "precision@k"),
    ("ndcg", "nDCG@k"),
    ("cost", "成本"),
    ("completionTokens", "输出 token"),
]


def aggregate_repeats(reports: list[dict[str, Any]]) -> dict[str, dict[str, dict]]:
    """把 N 次重跑按 (变体, 指标) 聚成离散度。各变体逐指标 metrics.dispersion。"""
    names = [s["variant"] for s in reports[0].get("summaries", [])]
    by_variant: dict[str, dict[str, dict]] = {}
    for name in names:
        per_metric: dict[str, dict] = {}
        for key, _label in _VARIANCE_METRICS:
            values: list[float] = []
            for report in reports:
                summary = next(
                    (s for s in report.get("summaries", []) if s["variant"] == name), None
                )
                value = _pick(summary, key) if summary else None
                if value is not None:
                    values.append(float(value))
            disp = metrics.dispersion(values)
            if disp is not None:
                per_metric[key] = disp
        by_variant[name] = per_metric
    return by_variant


def render_variance(variance: dict[str, Any]) -> list[str]:
    """多次重跑的方差小节。只在 repeat>1 时拼进报告。"""
    repeat, temp = variance["repeat"], variance["temperature"]
    label = {key: lbl for key, lbl in _VARIANCE_METRICS}
    lines = [
        "",
        f"## 多次运行方差（repeat={repeat}，temperature={temp}）",
        "",
        "同一套题重跑 N 次看抖动。eval 默认温度 0 几乎不抖；温度 >0 才量得出线上那种波动。",
        "**cv（变异系数 = stdev/mean）跨指标可比**：cv 大 = 这个变体单次回答最不可预测，",
        "均值再好看，上线后任意一次都可能明显更差——这是单跑一次的点估计看不到的。",
    ]
    for name, per_metric in variance["byVariant"].items():
        if not per_metric:
            continue
        lines += ["", f"### {name}", "", "| 指标 | 均值 | stdev | cv | 最小–最大 |",
                  "| --- | --- | --- | --- | --- |"]
        for key, disp in per_metric.items():
            cv = f"{disp['cv']:.3f}" if disp["cv"] is not None else "-"
            lines.append(
                f"| {label.get(key, key)} | {disp['mean']:.3f} | {disp['stdev']:.3f} | "
                f"{cv} | {disp['min']:.3f}–{disp['max']:.3f} |"
            )
    return lines


async def main() -> None:
    utf8_output()  # 报告渲染里有 `⚠`，GBK 控制台上会直接崩在打印那一刻
    parser = argparse.ArgumentParser(description="RAG 配置变体评估")
    parser.add_argument(
        "--variants",
        default="baseline",
        help=f"逗号分隔，或 all。可用：{', '.join(VARIANTS)}",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个问题")
    parser.add_argument(
        "--repeat", type=int, default=1,
        help="同一套题重跑几次看方差（默认 1）。>1 时报告追加「多次运行方差」小节",
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0,
        help="答案生成温度（默认 0.0 保持可复现；量方差时设 >0，如 0.7 贴近线上）",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="改用别的数据集文件（如 eval/datasets/feedback_regression.jsonl）",
    )
    parser.add_argument("--out", default=_REPORT_DIR, help="报告输出目录")
    parser.add_argument("--quiet", action="store_true", help="只输出报告，不打进度")
    parser.add_argument(
        "--render",
        default=None,
        metavar="JSON",
        help="不跑评估，只从已有的 JSON 重新生成 Markdown（零成本），默认打到标准输出",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="配合 --render：把结果覆盖写回同名 .md（默认不写，只打印）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )

    # 从已有 JSON 重渲染。这条路径存在的理由:**JSON 是事实,Markdown 只是它的
    # 一个视图**。此前改进报告只能靠重跑评估——而评估要花钱,于是渲染层的问题
    # 就一直攒着不修(precision、降级、语料指纹都是这么攒下来的)。有了这条,
    # 一次运行的成本可以摊到后面任意多次重渲染上,历史报告也重新变得可分析。
    #
    # 默认只打印、不写文件:覆盖同名 .md 是不可逆的,而重渲染最常见的用途恰恰是
    # "先看看新渲染器在老数据上是什么样"。要落盘再加 --write。
    if args.render:
        with open(args.render, encoding="utf-8") as handle:
            report = json.load(handle)
        markdown = render_markdown(report)
        if report.get("variance"):
            markdown += "\n" + "\n".join(render_variance(report["variance"]))
        print(markdown)
        if args.write:
            md_path = os.path.splitext(args.render)[0] + ".md"
            with open(md_path, "w", encoding="utf-8") as handle:
                handle.write(markdown)
            print(f"已覆盖写回：{md_path}")
        return

    variants = resolve([name.strip() for name in args.variants.split(",") if name.strip()])
    cases = runner.load_cases(args.limit, dataset_path=args.dataset)
    if not cases:
        raise SystemExit("金标准集为空")

    repeat = max(1, args.repeat)
    reports: list[dict[str, Any]] = []
    for index in range(repeat):
        reports.append(await runner.run(variants, cases, answer_temperature=args.temperature))
        if repeat > 1 and not args.quiet:
            logging.info("[repeat %d/%d] 完成", index + 1, repeat)

    report = reports[-1]
    markdown = render_markdown(report)
    if repeat > 1:
        # 方差是跨 N 次重跑算的，所以挂在最后一份报告上一并落盘，供 --render 重看
        report["variance"] = {
            "repeat": repeat,
            "temperature": args.temperature,
            "byVariant": aggregate_repeats(reports),
        }
        markdown += "\n" + "\n".join(render_variance(report["variance"]))

    os.makedirs(args.out, exist_ok=True)
    stamp = app_now().strftime("%Y%m%d-%H%M%S")
    json_path = os.path.join(args.out, f"eval-{stamp}.json")
    md_path = os.path.join(args.out, f"eval-{stamp}.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(markdown)

    print()
    print(markdown)
    print(f"逐题明细：{json_path}")


if __name__ == "__main__":
    asyncio.run(main())
