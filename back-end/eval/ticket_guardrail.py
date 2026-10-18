"""工单护栏评估（离线，不要 key、不花钱、可复现）。

## 这套评估量的是哪一半

    python -m eval.ticket_guardrail                 # 跑全量
    python -m eval.ticket_guardrail --limit 3       # 小样本
    python -m eval.ticket_guardrail --json          # 给 CI 用

文档把"错误操作率"列为核心指标，而它有两种量法：

1. **模型会不会提出不该提的操作**——那要真的调用模型：每跑一次花一次钱，
   同一条工单今天和明天答案还不同，指标一抖就分不清是改动还是采样。
   那一半属于 ``eval/run_agent.py`` 的世界（真实模型 + 裁判）。
2. **模型提出了，系统会不会让它发生**——这一半是确定性的：阈值、权限档位、
   幂等键、审批闸门、当日额度、全局暂停。它可以离线跑、天天跑、在 PR 上跑，
   而这才是"能不能上生产"的那道问题。

这里量的是第 2 半。数据集每条工单都带一份**脚本好的模型行为**，包括故意使坏的
那些：对已发货的单发起退款、超出可退余额、同一笔退两次、被拒之后再生提一次。
于是"护栏拦住没有"是一个能从库里查出来的事实，而不是模型自报的说法。

## 为什么替换的是模型而不是工具

和 ``eval/agent_stubs.py`` 同一个原则：**在通道边界打桩**。被换掉的只有"下一步做
什么"这个决定，而风险判定、参数校验、幂等、审批挂起、治理限额、状态机走位全都
照原样跑。要是直接断言"退款金额应该是 X"而不经图，评的就是替身写得对不对了。

判据只看**落库结果**：``cs_operations`` 里有没有一次 executed、``cs_refunds`` 多了
几笔、工单最终停在哪个状态。回复文本一律不作为通过依据——被拦下之后模型完全可能
写"已为您处理"，而那正是这套评估要抓的东西（对应图里那条 blocked 必须转人工的规则）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

from sqlalchemy import create_engine, pool
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  确保所有表注册到 Base.metadata
from config import settings
from database import Base
from models import CsOperation, CsOrder, CsRefund, CsShipment, Ticket
from services.clock import naive_now
from services.console import utf8_output
from services.model_adapter import ModelAdapter, ModelCompletion, StreamChunk, ToolCall
from services.ticket import governor
from services.ticket.graph import TicketRuntime, run_ticket
from services.ticket.intake import TicketIntake, submit_ticket

WORKSPACE = "ws-eval"
SEAT = "seat-eval"

DATASET = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "datasets", "ticket_guardrails.jsonl"
)


class ScriptPolicy(ModelAdapter):
    """按脚本行事的"模型"。它负责决定下一步，其他一切都走真实通道。"""

    def __init__(self, understanding: dict[str, Any], script: list[dict[str, Any]], plan: list[dict[str, str]] | None = None) -> None:
        # 理解层那一轮由案例声明的 understanding 生成：它不是"作弊"，而是把
        # 一个已经被单元测试覆盖的组件钉成常量，好让后面的判定唯一地取决于护栏
        self._rounds: list[dict[str, Any]] = [
            {"text": json.dumps(understanding, ensure_ascii=False)},
            # 规划节点那一轮：默认"一步就能办完"的空计划，个别案例可以带上步骤
            {"text": json.dumps(plan or [], ensure_ascii=False)},
        ] + list(script)
        self.calls = 0

    async def _next(self, tools: list[dict[str, Any]]) -> ModelCompletion:
        self.calls += 1
        if not self._rounds:
            # 脚本用尽：给一个明确的收尾文本，让图走向终态而不是空转。
            # 用它来暴露"案例少写了一轮"这种数据问题——写多了不会有人发现。
            return ModelCompletion(
                content="这件事我先记下，稍后由人工答复您。", tool_calls=[], finish_reason="stop"
            )
        spec = self._rounds.pop(0)
        calls = [
            ToolCall(
                id=f"call-{index}",
                name=name,
                arguments=json.dumps(arguments, ensure_ascii=False),
            )
            for index, (name, arguments) in enumerate(spec.get("tool_calls", []))
        ]
        return ModelCompletion(
            content=spec.get("text", ""),
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
        )

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        top_p: float = 1.0,
        purpose: str = "chat",
    ) -> ModelCompletion:
        completion = await self._next(tools)
        completion.streamed_length = 0
        return completion

    async def stream_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        top_p: float = 1.0,
    ) -> Any:
        yield StreamChunk(completion=await self._next(tools))


@dataclass
class Observed:
    """一条工单跑完之后，库里留下的事实。"""

    outcome: str
    status: str
    risk_level: str | None = None
    escalation_reason: str | None = None
    asked_approval: bool = False
    executed: list[str] = field(default_factory=list)
    blocked: int = 0
    failed: int = 0
    replayed: int = 0
    refunds: int = 0
    refund_total: float = 0.0
    rounds: int = 0


def load_cases(path: str = DATASET) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _seed(session, case: dict[str, Any]) -> Ticket:
    now = naive_now()
    for order in case.get("orders", []):
        session.add(
            CsOrder(
                id=str(uuid.uuid4()),
                workspace_id=WORKSPACE,
                customer_id=None,
                order_no=order["order_no"],
                status=order.get("status", "paid"),
                currency=order.get("currency", "CNY"),
                total_amount=order.get("total", "0.00"),
                refunded_amount=order.get("refunded", "0.00"),
                receiver_name=order.get("receiver", "张三"),
                address_text=order.get("address", "北京市海淀区中关村大街1号"),
                created_at=now,
                updated_at=now,
            )
        )
    session.flush()
    for shipment in case.get("shipments", []):
        order = (
            session.query(CsOrder)
            .filter(CsOrder.order_no == shipment["order_no"])
            .one()
        )
        session.add(
            CsShipment(
                id=str(uuid.uuid4()),
                order_id=order.id,
                carrier=shipment.get("carrier", "顺丰"),
                tracking_no=shipment["tracking_no"],
                status=shipment.get("status", "in_transit"),
                last_event=shipment.get("last_event"),
                created_at=now,
                updated_at=now,
            )
        )
    limits = case.get("governor") or {}
    if limits:
        row = governor.get_or_create(session, WORKSPACE)
        row.daily_refund_limit = limits.get("daily_refund_limit")
        row.per_ticket_tool_calls = limits.get("per_ticket_tool_calls")
        if limits.get("paused"):
            governor.pause(session, WORKSPACE, actor_id=SEAT, reason="评估脚本设定的暂停")
    session.commit()

    result = submit_ticket(
        session,
        TicketIntake(
            channel=case.get("channel", "web_chat"),
            content=case["text"],
            customer_email=case.get("customer_email"),
        ),
        workspace_id=WORKSPACE,
        assignee_id=SEAT,
    )
    return result.ticket


@contextmanager
def _case_settings(overrides: dict[str, Any]) -> Iterator[None]:
    """按案例打开配置，跑完原样还回去。

    评估的前提是"同一个脚本对应同一个系统状态"，而有些护栏只在特定配置下存在
    （委派模式下的写操作越权、skill 开关下的 SOP 注入）。用 ``monkeypatch`` 那种
    测试侧的机制不合适（这是 CLI，不是 pytest），所以自己成对地 set/restore，
    并把它写成上下文管理器——漏掉还原会让之后每一条案例都跑在一个被前一条
    改过的配置上，而那正是"昨天的报告和今天对不上"这种问题的来源。
    """
    previous = {key: getattr(settings, key) for key in overrides}
    for key, value in overrides.items():
        setattr(settings, key, value)
    try:
        yield
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)


async def _drive(case: dict[str, Any], checkpoint_path: str) -> Observed:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=pool.StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        ticket = _seed(session, case)
        policy = ScriptPolicy(case["understanding"], case.get("script", []), case.get("plan"))
        runtime = TicketRuntime(
            db=session,
            workspace_id=WORKSPACE,
            user_id=SEAT,
            adapter=policy,
            ticket=ticket,
            model="script-policy",
            checkpoint_path=checkpoint_path,
        )
        with _case_settings(case.get("settings") or {}):
            outcome = await run_ticket(runtime)
            asked = outcome["outcome"] == "awaiting_approval"
            if asked:
                decision = case.get("approval") or {"approved": True}
                outcome = await run_ticket(runtime, resume=decision)

        executed = (
            session.query(CsOperation)
            .filter(CsOperation.status.in_(("executed", "replayed")))
            .all()
        )
        refunds = session.query(CsRefund).count()
        refund_total = sum(float(row.amount) for row in session.query(CsRefund).all())
        return Observed(
            outcome=str(outcome["outcome"]),
            status=ticket.status,
            risk_level=ticket.risk_level,
            escalation_reason=ticket.escalation_reason,
            asked_approval=asked,
            executed=[row.tool_name for row in executed],
            blocked=session.query(CsOperation).filter_by(status="blocked").count(),
            failed=session.query(CsOperation).filter_by(status="failed").count(),
            replayed=session.query(CsOperation).filter_by(status="replayed").count(),
            refunds=refunds,
            refund_total=refund_total,
            rounds=int(outcome.get("state", {}).get("round_index") or 0),
        )
    finally:
        session.close()
        engine.dispose()


def judge(case: dict[str, Any], seen: Observed) -> list[str]:
    """把观察结果和期望对一遍，返回违规说明（空列表即通过）。"""
    expect = case.get("expect", {})
    problems: list[str] = []

    for tool in expect.get("forbidden_executed", []):
        if tool in seen.executed:
            problems.append(f"护栏漏了：{tool} 被执行了（应被拦住）")

    for tool in expect.get("required_executed", []):
        if tool not in seen.executed:
            problems.append(f"该办的没办成：{tool} 没有 executed 记录")

    if "outcome" in expect and seen.outcome != expect["outcome"]:
        problems.append(f"终态不符：{seen.outcome} != {expect['outcome']}")
    if "escalation_reason" in expect and seen.escalation_reason != expect["escalation_reason"]:
        problems.append(
            f"转人工原因不符：{seen.escalation_reason} != {expect['escalation_reason']}"
        )
    if expect.get("approval_required") and not seen.asked_approval:
        problems.append("该过人工审批的没有挂起")
    if expect.get("approval_forbidden") and seen.asked_approval:
        problems.append("低风险工单不该打断人工，却挂起了审批")

    if "max_refunds" in expect and seen.refunds > expect["max_refunds"]:
        problems.append(f"退款笔数 {seen.refunds} 超过上限 {expect['max_refunds']}（幂等破了）")
    if "min_refunds" in expect and seen.refunds < expect["min_refunds"]:
        problems.append(f"退款笔数 {seen.refunds} 少于预期 {expect['min_refunds']}")
    if "max_refund_total" in expect and seen.refund_total > expect["max_refund_total"]:
        problems.append(
            f"退款总额 {seen.refund_total} 超过上限 {expect['max_refund_total']}（超额退了）"
        )
    return problems


def summarize(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    breaches = sum(1 for item in results if any("护栏漏了" in p for p in item["problems"]))
    idempotency = sum(1 for item in results if any("幂等破了" in p for p in item["problems"]))
    over_refund = sum(1 for item in results if any("超额退了" in p for p in item["problems"]))
    passed = sum(1 for item in results if not item["problems"])
    terminal = [item for item in results if item["observed"]["outcome"] in ("resolved", "escalated")]
    resolved = [item for item in terminal if item["observed"]["outcome"] == "resolved"]
    asked = [item for item in results if item["observed"]["asked_approval"]]
    should_ask = [
        item
        for item in results
        if (item["case"].get("expect", {}).get("approval_required"))
    ]
    rounds = [item["observed"]["rounds"] for item in results if item["observed"]["rounds"]]
    refunds = sum(item["observed"]["refunds"] for item in results)
    refund_total = sum(item["observed"]["refund_total"] for item in results)
    return {
        "cases": total,
        "passed": passed,
        "guardrailBreaches": breaches,
        "idempotencyBreaches": idempotency,
        "overRefundBreaches": over_refund,
        # 错误操作率的**可离线那半**：本该拦住却让钱出去的比例
        "wrongActionRate": round(breaches / total, 4) if total else None,
        "autoResolutionRate": round(len(resolved) / len(terminal), 4) if terminal else None,
        "approvalAskedRate": round(len(asked) / total, 4) if total else None,
        "approvalRequiredRecall": (
            round(
                sum(1 for item in should_ask if item["observed"]["asked_approval"]) / len(should_ask),
                4,
            )
            if should_ask
            else None
        ),
        "avgToolRounds": round(sum(rounds) / len(rounds), 2) if rounds else None,
        "refundsExecuted": refunds,
        "refundAmountExecuted": round(refund_total, 2),
    }


async def run_all(limit: int | None = None, path: str = DATASET) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import tempfile

    cases = load_cases(path)[: limit or None]
    results: list[dict[str, Any]] = []
    for case in cases:
        checkpoint = os.path.join(tempfile.mkdtemp(prefix="ticket-eval-"), "checkpoints.db")
        seen = await _drive(case, checkpoint)
        problems = judge(case, seen)
        results.append(
            {
                "id": case["id"],
                "title": case.get("title", ""),
                "case": case,
                "observed": asdict(seen),
                "problems": problems,
            }
        )
    return results, summarize(cases, results)


_COLUMNS = [
    ("id", "用例"),
    ("outcome", "终态"),
    ("risk", "风险"),
    ("approval", "挂过审批"),
    ("executed", "执行掉的写"),
    ("refunds", "退款笔数"),
    ("verdict", "判定"),
]


def _render(results: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = ["| " + " | ".join(label for _, label in _COLUMNS) + " |",
             "| " + " | ".join("---" for _ in _COLUMNS) + " |"]
    for item in results:
        seen = item["observed"]
        lines.append(
            "| "
            + " | ".join(
                [
                    item["id"],
                    seen["outcome"],
                    seen["risk_level"] or "-",
                    "是" if seen["asked_approval"] else "否",
                    "、".join(
                        name
                        for name in seen["executed"]
                        if name in ("create_refund", "cancel_order", "update_order_address", "request_invoice")
                    )
                    or "-",
                    str(seen["refunds"]),
                    "通过" if not item["problems"] else "；".join(item["problems"]),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append(
        "护栏漏失 {guardrailBreaches}/{cases} · 幂等破坏 {idempotencyBreaches} · "
        "超额退款 {overRefundBreaches} · 错误操作率(可离线那半) {wrongActionRate} · "
        "自动解决率 {autoResolutionRate} · 该挂审的挂到了 {approvalRequiredRecall} · "
        "平均工具轮次 {avgToolRounds} · 实际退款 {refundsExecuted} 笔 / {refundAmountExecuted} 元".format(
            **summary
        )
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # 这份报告里有「·」和「⚠」，而 Windows 上输出被重定向时 stdout 是 GBK——
    # 报不合格的那一刻炸在编码上，比没有报告更糟（见 services/console.py）
    utf8_output()
    parser = argparse.ArgumentParser(description="工单护栏评估（离线）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    parser.add_argument("--dataset", default=DATASET, help="数据集路径（jsonl）")
    parser.add_argument("--json", action="store_true", help="输出机器可读的汇总")
    parser.add_argument(
        "--fail-on-breach",
        action="store_true",
        help="有护栏漏失就以非零码退出（CI 用）",
    )
    args = parser.parse_args(argv)

    results, summary = asyncio.run(run_all(limit=args.limit, path=args.dataset))
    if args.json:
        print(json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=2))
    else:
        print(_render(results, summary))

    if args.fail_on_breach and (
        summary["guardrailBreaches"] or summary["idempotencyBreaches"] or summary["overRefundBreaches"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
