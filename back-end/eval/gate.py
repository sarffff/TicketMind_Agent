"""评估门禁：读一份报告 JSON，判它是否达标，不达标以非零退出。

## 为什么单独一个脚本，而不是让 run.py 自己判

三个理由：

1. **可以对历史报告回放。** 阈值改了之后，"按新阈值，上周那份还过不过"是个必须能
   当场回答的问题。门禁与运行分开才做得到。
2. **本地和 CI 判的是同一件事。** 同一个脚本、同一个阈值文件，"我这儿是过的"
   才有意义。
3. **run.py 的职责是产出测量。** 让它同时决定"什么算不合格"，会让一次阈值调整
   变成动评估代码。

## 三类阈值，处置完全不同

- **health**（运行可信度）不达标时，报告里的质量指标**没有意义**，此时报"质量回归"
  是误诊。2026-08-27 那份就是这个形状：211 次 429 打空了 54/54 个答案，所有质量列
  归零，看起来像模型彻底坏了。所以 health 先判，且失败时**不再往下判质量**——
  免得在输出里堆一屏由同一个原因导致的假回归。
- **security**（抗注入、编造）是硬门，等号是唯一可接受值。
- **quality** 留足余量，抓"明显坏了"而不是"动了一点"。

## 退出码

0 达标；1 质量或安全不达标；2 运行不可信（health）；3 用法错误（文件缺失、
变体不存在、JSON 坏了）。分开是为了让 CI 能区分"要查模型"和"要重跑"。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from eval import problem_labels
from services.console import utf8_output

# 逐条点名要在 problems 里找的前缀。绑定只发生在模块级这一处，所以
# tests/test_eval_gate.py 能断言 `gate._BREACH_LABELS is problem_labels.BREACHES`——
# 断 `is` 不判 `==`，因为等于一份的副本照样会各自漂。这份名单同时是
# ticket_behavior.summarize 用来计数的键，两处手写之后报告里的数字和这里点名的
# 用例就不是同一件事了，而两边都照样跑成功、照样退 0。
_BREACH_LABELS = problem_labels.BREACHES


def _counted(item: dict[str, Any]) -> bool:
    """这条用例算不算进汇总数字。判据是生产者写进报告的 `measured` 字段：
    做决定的那一步（``ticket_agent``）有没有真的答过。

    缺字段（2026-10-17 之前的报告）按算进去处理——那时 summarize 的分母就是全量，
    数字和被点名的用例本来就一致，用今天的判据去回判反而会让回放对不上。
    """
    if "measured" not in item:
        return True
    return bool(item["measured"])

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_THRESHOLDS = os.path.join(_HERE, "gate_thresholds.json")
_REPORT_DIR = os.path.join(_HERE, "reports")

EXIT_OK = 0
EXIT_QUALITY = 1
EXIT_UNTRUSTWORTHY = 2
EXIT_USAGE = 3


@dataclass
class Check:
    """一条判定。``ok`` 为假时 ``detail`` 说明差多少。"""

    name: str
    ok: bool
    actual: Any
    limit: Any
    kind: str  # min / max / eq
    category: str

    @property
    def detail(self) -> str:
        symbol = {"min": "≥", "max": "≤", "eq": "="}[self.kind]
        return f"{self.name}: 实测 {_fmt(self.actual)}，要求 {symbol} {_fmt(self.limit)}"


@dataclass
class Verdict:
    variant: str
    checks: list[Check] = field(default_factory=list)

    @property
    def health_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.category == "health"]

    @property
    def other_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.category != "health"]

    @property
    def exit_code(self) -> int:
        if self.health_failures:
            return EXIT_UNTRUSTWORTHY
        return EXIT_QUALITY if self.other_failures else EXIT_OK


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _num(value: Any) -> float | None:
    """把指标取成数。None 与非数一律返回 None，由调用方决定怎么处置。

    **缺失不当成 0。** 报告里某一列为空的原因通常是"这次没算"，而不是"算出来是
    零分"——把它当 0 会让一次字段改名变成一屏假回归。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _check_min(name, actual, limit, category, checks) -> None:
    value = _num(actual)
    # 缺失即不达标，但 detail 里会显示 "—"，好和"真的低了"区分开
    checks.append(
        Check(name, value is not None and value >= limit, actual, limit, "min", category)
    )


def _check_max(name, actual, limit, category, checks) -> None:
    value = _num(actual)
    # 上界类指标缺失按 0 处理：degradedCases 之类的计数，报告里没有这个键
    # 通常就是真的没发生（run.py 只在非零时才写某些字段）。
    if value is None:
        value = 0.0
    checks.append(Check(name, value <= limit, value, limit, "max", category))


def evaluate(summary: dict[str, Any], thresholds: dict[str, Any]) -> Verdict:
    """判一个变体的 summary。返回全部判定（含通过的，好让输出能显示余量）。"""
    variant = str(summary.get("variant") or summary.get("name") or "?")
    verdict = Verdict(variant=variant)
    checks = verdict.checks

    health = thresholds.get("health") or {}
    if "maxDegradedCases" in health:
        _check_max("降级题数", summary.get("degradedCases"), health["maxDegradedCases"], "health", checks)
    if "maxJudgeFailures" in health:
        _check_max("裁判解析失败", summary.get("judgeFailures"), health["maxJudgeFailures"], "health", checks)
    if "maxJudgeInconsistent" in health:
        _check_max("裁判自相矛盾", summary.get("judgeInconsistent"), health["maxJudgeInconsistent"], "health", checks)
    if "minQuestions" in health:
        _check_min("题目数", summary.get("questions"), health["minQuestions"], "health", checks)
    if "minRetrievalScored" in health:
        _check_min("打分题数", summary.get("retrievalScored"), health["minRetrievalScored"], "health", checks)
    if "corpusChunks" in health:
        actual = _num(summary.get("corpusChunks"))
        expected = health["corpusChunks"]
        # 相等判定：分块数变了说明索引不是同一个，检索指标与基线不可比
        checks.append(
            Check("语料分块数", actual == float(expected), summary.get("corpusChunks"), expected, "eq", "health")
        )

    security = thresholds.get("security") or {}
    if "minInjectionResistRate" in security:
        _check_min("抗注入率", summary.get("injectionResistRate"), security["minInjectionResistRate"], "security", checks)
    if "maxFabricationRate" in security:
        _check_max("编造率", summary.get("fabricationRate"), security["maxFabricationRate"], "security", checks)

    quality = thresholds.get("quality") or {}
    mapping = [
        ("minRecallAt5", "recall@5", "recall@5"),
        ("minPrecisionAt5", "precision@5", "precision@5"),
        ("minNdcgAt5", "ndcg@5", "ndcg@5"),
        ("minMrr", "mrr", "mrr"),
        ("minKeywordCoverage", "keywordCoverage", "关键词覆盖"),
        ("minFaithfulness", "faithfulness", "忠实度"),
        ("minRelevance", "relevance", "相关性"),
        ("minAbstentionRate", "abstentionRate", "拒答率"),
    ]
    for key, field_name, label in mapping:
        if key in quality:
            _check_min(label, summary.get(field_name), quality[key], "quality", checks)

    return verdict


TICKET_SUMMARY_KEY = "ticket"


def evaluate_ticket(summary: dict[str, Any], thresholds: dict[str, Any]) -> Verdict:
    """判一次工单行为评估（``eval.ticket_behavior --json`` 的 ``summary``）。

    和 RAG 那侧的区别只在有没有基线：

    * health：这次运行到底跑没跑完。跑了一半的报告读起来像"没问题"，
      而真实情况是"还没测"。判的是 `measured`（做决定的那一步真的答过的条数）而不
      只是 `cases`（跑了多少条）——一条从没答过的工单不是样本，是缺席。
    * safety：对客户说了一句台账里没有的话（空头承诺）、提出一笔超过可退余额的退款
      （超额提案）。这两件事没有"容忍度"可言——一次漏发审批、一次多退钱，都不是
      质量波动而是事故，所以等号就是唯一可接受值。0 是观测到的值，不是模型的性质，
      别把它读成"这一侧不会出事"。
    * quality：``proposalBreaches``（提了场景外的工具、该提的没提、提案次数超预算）。
      判，但留余量。这不是让步而是测量学：同一套用例连着跑三轮，这个数字本身会抖，
      而里面还混着一条**稳定复现**的过度提案缺陷——按"看到 1 条就判红"设阈值，
      门禁会为一个没有任何回归的正常轮次亮红，那种红第二天就被静音，比没有门禁更糟。
      余量留几条、其中几条是被缺陷长期占着的、什么时候该收回，全在
      gate_thresholds.json 的 `ticket` 段里。**这里刻意不复述那些数字**：这个函数
      上一版就是这么写的，等三轮重测完，文档里的数就成了假数——一份会漂的地方
      不该存在两份。
    """
    verdict = Verdict(variant="ticket")
    checks = verdict.checks
    section = thresholds.get(TICKET_SUMMARY_KEY) or {}

    health = section.get("health") or {}
    if "minCases" in health:
        _check_min("工单条数", summary.get("cases"), health["minCases"], "health", checks)
    if "minMeasured" in health:
        # 数据集有 20 条，但其中被模型通道抛错截掉的那些**不是样本**，是缺席。
        # 拿 cases 当分母读阈值会让一份 18/20 的运行看起来和满员运行一样可信，
        # 而软门那条容忍数恰恰是按满员标的
        _check_min("有效样本", summary.get("measured"), health["minMeasured"], "health", checks)
    if "maxJudgeFailures" in health:
        _check_max(
            "裁判不可用", summary.get("judgeFailed"), health["maxJudgeFailures"], "health", checks
        )
    if "minModelCalls" in health:
        # 一次调用都没发生 = 这条路径压根没跑到，而不是"模型很乖没提任何东西"
        _check_min(
            "模型调用总数",
            (summary.get("avgModelCalls") or 0) * (summary.get("cases") or 0),
            health["minModelCalls"],
            "health",
            checks,
        )

    safety = section.get("safety") or {}
    if "maxOverClaimBreaches" in safety:
        _check_max(
            "空头承诺", summary.get("overClaimBreaches"), safety["maxOverClaimBreaches"], "security", checks
        )
    if "maxOverAmountProposals" in safety:
        _check_max(
            "超额退款提案",
            summary.get("overAmountProposals"),
            safety["maxOverAmountProposals"],
            "security",
            checks,
        )

    quality = section.get("quality") or {}
    if "maxProposalBreaches" in quality:
        _check_max(
            "提案越界", summary.get("proposalBreaches"), quality["maxProposalBreaches"], "quality", checks
        )

    return verdict


def cross_variant_checks(
    summary: dict[str, Any], thresholds: dict[str, Any]
) -> list[Check]:
    """对未被 gate 的变体判"这次运行是不是被打坏了"。

    比 ``health`` 松得多，而且按**比例**判而不是按绝对条数：对照组偶尔降级几题
    是它自己的性质（本地 cross-encoder 会超时），而"超过四分之一的题坏掉"是
    系统性故障——此时同一批运行里其他变体的数字也不该当成可信结论。

    ``minPromptTokens`` 抓的是另一个更硬的形状：答案全空时输入 token 会从十万
    量级塌到三位数。它比降级计数更早可见——降级要等重试耗尽才记账。
    """
    config = thresholds.get("crossVariant") or {}
    checks: list[Check] = []
    if not config:
        return checks

    questions = _num(summary.get("questions")) or 0.0
    if questions > 0:
        if "maxDegradedRate" in config:
            rate = (_num(summary.get("degradedCases")) or 0.0) / questions
            checks.append(
                Check(
                    f"降级比例（{_fmt(summary.get('degradedCases'))}/{int(questions)}）",
                    rate <= config["maxDegradedRate"],
                    rate,
                    config["maxDegradedRate"],
                    "max",
                    "systemic",
                )
            )
        if "maxJudgeFailureRate" in config:
            rate = (_num(summary.get("judgeFailures")) or 0.0) / questions
            checks.append(
                Check(
                    f"裁判失败比例（{_fmt(summary.get('judgeFailures'))}/{int(questions)}）",
                    rate <= config["maxJudgeFailureRate"],
                    rate,
                    config["maxJudgeFailureRate"],
                    "max",
                    "systemic",
                )
            )
    if "minPromptTokens" in config:
        _check_min(
            "输入 token 总量",
            summary.get("promptTokens"),
            config["minPromptTokens"],
            "systemic",
            checks,
        )
    return checks


def render(verdicts: list[Verdict]) -> str:
    lines: list[str] = []
    for verdict in verdicts:
        failures = verdict.health_failures or verdict.other_failures
        status = "通过" if not failures else "不达标"
        lines.append(f"变体 {verdict.variant}：{status}")
        if verdict.health_failures:
            lines.append("  运行不可信（以下指标不达标时，质量列不可读）：")
            for check in verdict.health_failures:
                lines.append(f"    ✗ {check.detail}")
            lines.append("  已跳过质量判定——先把运行本身修好，再看质量。")
            continue
        for check in verdict.other_failures:
            lines.append(f"    ✗ [{check.category}] {check.detail}")
        if not failures:
            # 通过时也把最接近阈值的三条打出来：知道余量还剩多少，比只知道"过了"有用
            margins = []
            for check in verdict.checks:
                value, limit = _num(check.actual), _num(check.limit)
                if value is None or limit is None or check.kind != "min":
                    continue
                margins.append((value - limit, check))
            margins.sort(key=lambda item: item[0])
            for margin, check in margins[:3]:
                lines.append(f"    · 余量最小 {check.name}: {_fmt(check.actual)}（阈值 {_fmt(check.limit)}，余 {margin:+.4f}）")
    return "\n".join(lines)


def latest_report(directory: str) -> str | None:
    """目录里最新那份 eval-*.json。"""
    if not os.path.isdir(directory):
        return None
    names = sorted(
        name
        for name in os.listdir(directory)
        if name.startswith("eval-") and name.endswith(".json")
    )
    return os.path.join(directory, names[-1]) if names else None


def _gate_ticket(report: dict[str, Any], thresholds: dict[str, Any], path: str) -> int:
    summary = report.get("summary") or {}
    print(f"报告：{os.path.basename(path)}")

    # 先报"哪些条没算"，再判阈值。以前这里看到 unmeasured 非零就直接退 2，
    # 等于把容忍度写死在门禁代码里。缺席多少条能接受是阈值该说的话
    # （health.minMeasured），只有一个地方能改，也才只会被改一次。
    dropped = [item for item in (report.get("cases") or []) if not _counted(item)]
    if dropped:
        print(
            f"剔除 {len(dropped)}/{summary.get('cases', len(dropped))} 条"
            f"（做决定的那一步没答过）：{', '.join(str(item.get('id')) for item in dropped)}"
        )
        print("  它们既没通过也没越界——没算进下面的数字。")

    verdict = evaluate_ticket(summary, thresholds)
    print(render([verdict]))

    waived = [
        (item.get("id"), p)
        for item in report.get("cases") or []
        for p in item.get("problems") or []
        if p.startswith(problem_labels.WAIVED)
    ]
    if waived:
        # 免判不是没事。读一份报告的人要先知道这一轮被网络吃掉了多少条判项，
        # 否则"越界 0"会被读成"模型这次很准"
        print()
        print(f"因模型通道抛错免判了 {len(waived)} 条「没提该提的」：")
        for case_id, problem in waived:
            print(f"    · [{case_id}] {problem}")

    breaches = [
        item for item in report.get("cases") or []
        if _counted(item) and any(p.startswith(_BREACH_LABELS) for p in item.get("problems") or [])
    ]
    if breaches:
        # 汇总数字只说"几条"，而修的人需要知道是哪几条。没有这一段的夜晚会有人
        # 对着 exit 1 猜半天。达标时也照打：软门容忍的那一条，得让人看得见是哪条
        # 才谈得上"下次别再加一条"。
        print()
        print("越界的是这些用例：")
        for item in breaches:
            for problem in item["problems"]:
                if problem.startswith(_BREACH_LABELS):
                    print(f"    ✗ [{item.get('id')}] {problem}")

    code = verdict.exit_code
    if code == EXIT_UNTRUSTWORTHY:
        print("→ 这次运行不可信（跑的条数、真正测到的条数或模型调用数不够），安全结论不可读。")
    elif code == EXIT_QUALITY:
        # 两类红要分开说，因为它们要修的东西完全不同：硬门是事故，去查护栏与提示词
        # 里的事实来源；软门是模型变激进了，先看是不是同一个用例连着翻
        hard = [c.name for c in verdict.other_failures if c.category == "security"]
        soft = [c.name for c in verdict.other_failures if c.category != "security"]
        if hard:
            print(f"→ 安全门禁不达标：{'、'.join(hard)}。这不是质量波动，是事故。")
        if soft:
            print(
                f"→ 质量门禁不达标：{'、'.join(soft)} 超出了实测抖动范围"
                "（余量多少条写在 gate_thresholds.json 的 ticket.quality，含它的适用边界）。"
            )
    return code


def main(argv: list[str] | None = None) -> int:
    # 门禁最不该失败的时机正是它要报不合格的那一刻，而渲染 verdict 会打 `✗`——
    # 那个字符不在 GBK 里，Windows 上输出被重定向时直接抛 UnicodeEncodeError
    # （见 services/console.py）
    utf8_output()
    parser = argparse.ArgumentParser(description="评估门禁：判一份报告是否达标")
    parser.add_argument(
        "report",
        nargs="?",
        help="报告 JSON 路径。省略时取 eval/reports 里最新那份",
    )
    parser.add_argument("--thresholds", default=_DEFAULT_THRESHOLDS, help="阈值文件")
    parser.add_argument(
        "--variant",
        action="append",
        help="只判这些变体（可重复）。省略时用阈值文件里的 variants.gated",
    )
    args = parser.parse_args(argv)

    path = args.report or latest_report(_REPORT_DIR)
    if not path or not os.path.isfile(path):
        print(f"找不到报告文件：{path or '(eval/reports 里没有 eval-*.json)'}", file=sys.stderr)
        return EXIT_USAGE

    try:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
        with open(args.thresholds, encoding="utf-8") as handle:
            thresholds = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取失败：{type(exc).__name__}", file=sys.stderr)
        return EXIT_USAGE

    # 两种报告共用这一个入口：RAG 那份是 {"summaries": [...]}（每变体一条），
    # 工单行为那份是 {"summary": {...}, "cases": [...]}。靠 summaries 在不在来分流，
    # 而不是靠文件名——定时任务里报告名带时间戳，靠名字分会哪天就分错。
    if "summaries" not in report and isinstance(report.get("summary"), dict):
        return _gate_ticket(report, thresholds, path)

    summaries = report.get("summaries") or []
    if not summaries:
        print("报告里没有 summaries，无法判定。", file=sys.stderr)
        return EXIT_USAGE

    wanted = args.variant or (thresholds.get("variants") or {}).get("gated") or []
    by_name = {
        str(s.get("variant") or s.get("name")): s for s in summaries
    }
    if not wanted:
        wanted = list(by_name)

    missing = [name for name in wanted if name not in by_name]
    if missing:
        print(
            f"报告里没有这些变体：{', '.join(missing)}。"
            f"报告含有：{', '.join(by_name)}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # health 判**全部变体**，quality/security 只判被 gate 的那些。
    #
    # 这个区分是写完第一版之后发现的：当时只判 baseline，拿 2026-08-27 那份
    # 429 报告一跑居然通过了——因为 429 打空的是 rerank-api（861 输入 token、
    # 54 次裁判失败）和 rerank（27 题降级），而 baseline 恰好跑完了。
    #
    # 但"三个变体里两个被打空"不是一次可信的运行，哪怕 baseline 活着：同一批
    # 限流既然能打空两个变体，就没有理由相信第三个变体的数字没被影响。
    # 质量门禁维持只判 baseline（对照组退化说明的是那个增强不划算，不是主链路坏了），
    # 可信度必须横跨全部。
    verdicts = [evaluate(by_name[name], thresholds) for name in wanted]
    print(f"报告：{os.path.basename(path)}")
    print(render(verdicts))

    others = [name for name in by_name if name not in wanted]
    systemic: list[tuple[str, Check]] = []
    for name in others:
        for check in cross_variant_checks(by_name[name], thresholds):
            if not check.ok:
                systemic.append((name, check))
    if systemic:
        print()
        print("同一批运行里其他变体出现系统性故障——被 gate 的变体即使达标，")
        print("也不能当成可信结论：")
        for name, check in systemic:
            print(f"    ✗ [{name}] {check.detail}")

    worst = EXIT_OK
    for verdict in verdicts:
        # 运行不可信优先于质量不达标：它决定了质量结论能不能读
        code = verdict.exit_code
        if code == EXIT_UNTRUSTWORTHY:
            worst = code
        elif code and worst == EXIT_OK:
            worst = code
    if systemic:
        worst = EXIT_UNTRUSTWORTHY
    return worst


if __name__ == "__main__":
    sys.exit(main())
