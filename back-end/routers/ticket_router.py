"""工单接口：提交、队列、审批、轨迹回放、治理开关与指标。

约定与其余 router 一致：只 ``Depends(get_current_user)``、返回 camelCase 的 dict、
service 抛自定义错误 → 这里一律 ``HTTPException(400, str(exc))``，不区分
"不存在"与"无权"（那条边界一旦泄露就变成"这张工单存在但你看不到"）。

路由本身**始终注册**，``TICKET_AGENT_ENABLED`` 只管行为不管有没有这些路径。理由和
``/fs`` 那套一样：前端要能区分"后端没开这个能力"和"开了但这条工单办不成"，
否则关着开关时界面只会得到一个 404，而用户以为是自己点错了。

提交与执行是**两个接口**，不是一个。一次工单执行要跑模型、要挂人审，它的生命
周期比一个 HTTP 请求长得多；把它捆在提交请求里，等于让"客户发的工单"取决于
"那一刻前端有没有断线"。提交只负责把工单变成一条持久记录并回执，
谁来驱动它、什么时候驱动，是队列的事。
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from config import settings
from database import get_db
from models import CsOperation, Ticket, TicketOutbox, User
from services import audit_log
from services.clock import naive_now
from services.knowledge_service import KnowledgeService
from services.model_adapter import OpenAICompatibleAdapter
from services.ticket import cs_import as ticket_cs_import
from services.ticket import governor, graph as ticket_graph, intake as ticket_intake
from services.ticket import history as ticket_history, ledger as ticket_ledger, sla as ticket_sla
from services.ticket import outbox as ticket_outbox, tools as ticket_tools
from services.ticket import senders as ticket_senders
from services.ticket.trace import append_event, replay as replay_events
from services.usage_guard import check as usage_check

router = APIRouter(prefix="/tickets", tags=["工单"])
logger = logging.getLogger("ticket_router")
# 和 knowledge_router 一样是模块级单例：检索通道自己持有嵌入客户端与索引，
# 每个请求重建一次等于每次重新连一遍
_knowledge = KnowledgeService()


def _make_adapter() -> OpenAICompatibleAdapter:
    """单独一个函数而不是就地 new：测试要把模型通道整段换成脚本回放的那个。"""
    return OpenAICompatibleAdapter()


def _make_sender():
    """对客户的发送通道。实现与"为什么现在返回 None"都在 ``services/ticket/senders.py``。

    留一个函数而不是直接用 ``senders.build_sender``：测试要把发送通道整段换成假的，
    而自动排空循环也要拿同一个东西——判断只有一份，接缝也只有一处。
    """
    return ticket_senders.build_sender()


class TicketSubmitBody(BaseModel):
    channel: str = Field(default="web_chat", description="渠道标签，须在白名单内")
    content: str = Field(min_length=1, description="客户原文")
    subject: str | None = None
    external_ref: str | None = Field(default=None, description="渠道侧消息/邮件 ID，用于去重")
    customer_email: str | None = None
    customer_phone: str | None = None
    customer_ref: str | None = Field(default=None, description="APP/企微自家的用户 ID")
    attachments: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionBody(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=500)
    # {tool_call_id: {改动的键: 新值}}。只允许改已有键——那条规则住在
    # approval.validate_edit，两个入口各写一遍迟早会不一样
    edited: dict[str, dict[str, Any]] = Field(default_factory=dict)


class PauseBody(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class CsatBody(BaseModel):
    # 1..5 直接由 Field 卡住：评分是进指标的数，脏值一旦入库就要在所有
    # 聚合里防它
    score: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)


class CloseBody(BaseModel):
    resolution: str = Field(min_length=1, max_length=20, description="人工最终怎么解决的")
    note: str = Field(default="", max_length=1000)


class ReviewBody(BaseModel):
    # 结论的合法值由 ledger.record_review 校验（那里也是"只有执行过的才值得复盘"
    # 那条规则的所在地）。这里不做第二份枚举，避免两份清单对不上
    verdict: str
    action: str = Field(default="", max_length=40, description="判错之后实际怎么补救的")
    note: str = Field(default="", max_length=500)


def _require_enabled() -> None:
    if not settings.TICKET_AGENT_ENABLED:
        # 409 而不是 404：路径存在、能力没开，这是一个可以被答复的状态，
        # 前端能据此告诉用户"去设置里打开"，而不是显示一张空队列
        raise HTTPException(
            status_code=409,
            detail="工单 Agent 未启用（TICKET_AGENT_ENABLED=false）",
        )


def _runtime(db: Session, user: User, ticket: Ticket):
    """把工单执行需要的外部世界接起来。

    模型适配器与政策检索都在这里装配，而不是散在节点里：测试要能整段换掉它们，
    而"接哪个检索"是部署决定，不是流程决定。
    """

    async def policy_search(query: str) -> str:
        context, _citations = await _knowledge.build_rag_context_with_citations(
            db, query, user.workspace_id, top_k=settings.RAG_TOP_K, viewer_id=user.id
        )
        return context

    return ticket_graph.TicketRuntime(
        db=db,
        workspace_id=user.workspace_id,
        user_id=user.id,
        adapter=_make_adapter(),
        ticket=ticket,
        model=settings.LLM_MODEL,
        policy_search=policy_search,
        checkpoint_path=settings.TICKET_CHECKPOINT_DB,
    )


def _load_ticket(db: Session, user: User, ticket_id: str) -> Ticket:
    ticket = (
        db.query(Ticket)
        .filter(Ticket.id == ticket_id, Ticket.workspace_id == user.workspace_id)
        .first()
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return ticket


def _ticket_dict(ticket: Ticket, *, brief: bool = False) -> dict[str, Any]:
    def _loads(raw: str | None) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    data: dict[str, Any] = {
        "id": ticket.id,
        "channel": ticket.channel,
        "status": ticket.status,
        "riskLevel": ticket.risk_level,
        "intent": ticket.intent,
        "subject": ticket.subject,
        "summary": ticket.summary,
        "customerId": ticket.customer_id,
        "assigneeId": ticket.assignee_id,
        "resolution": ticket.resolution,
        "escalationReason": ticket.escalation_reason,
        "csatScore": ticket.csat_score,
        "toolRounds": ticket.tool_rounds,
        "createdAt": ticket.created_at.isoformat() if ticket.created_at else None,
        "updatedAt": ticket.updated_at.isoformat() if ticket.updated_at else None,
        "resolvedAt": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
        "slaDueAt": ticket.sla_due_at.isoformat() if ticket.sla_due_at else None,
        "firstResponseAt": (
            ticket.first_response_at.isoformat() if ticket.first_response_at else None
        ),
    }
    if not brief:
        data["requestText"] = ticket.request_text
        data["entities"] = _loads(ticket.entities)
        data["attachments"] = _loads(ticket.attachments) or []
        data["resolutionNote"] = ticket.resolution_note
        data["csatComment"] = ticket.csat_comment
        data["llmCost"] = str(ticket.llm_cost) if ticket.llm_cost is not None else None
        data["closedAt"] = ticket.closed_at.isoformat() if ticket.closed_at else None
    return data


@router.post("")
def submit_ticket(
    body: TicketSubmitBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _require_enabled()
    try:
        result = ticket_intake.submit_ticket(
            db,
            ticket_intake.TicketIntake(
                channel=body.channel,
                content=body.content,
                subject=body.subject,
                external_ref=body.external_ref,
                customer_email=body.customer_email,
                customer_phone=body.customer_phone,
                customer_ref=body.customer_ref,
                attachments=tuple(body.attachments),
                metadata=body.metadata,
            ),
            workspace_id=user.workspace_id,
            assignee_id=user.id,
        )
    except ticket_intake.TicketIntakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ticketId": result.ticket.id,
        "created": result.created,
        "dedupedAgainst": result.deduped_against,
        "customerId": result.customer_id,
        "status": result.ticket.status,
    }


@router.get("")
def list_tickets(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    status: str | None = None,
    risk: str | None = None,
    mine: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    # 超时的单子在这里被转走，而不是等一个定时器。这个仓库没有调度器，
    # agent_runs 的租约回收、document_jobs 的重试、审批过期清理走的都是同一条读路径
    # （见 approval_audit.expire_stale）。代价是"没人打开队列时不处理超时"，
    # 而那种时刻没有任何人被一张卡住的工单挡住。
    reaped = ticket_sla.reap_overdue(db, workspace_id=user.workspace_id, limit=20)
    query = db.query(Ticket).filter(Ticket.workspace_id == user.workspace_id)
    if status:
        query = query.filter(Ticket.status == status)
    if risk:
        query = query.filter(Ticket.risk_level == risk)
    if mine:
        query = query.filter(Ticket.assignee_id == user.id)
    total = query.count()
    rows = (
        query.order_by(Ticket.created_at.desc())
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
        .all()
    )
    return {
        "enabled": settings.TICKET_AGENT_ENABLED,
        "total": total,
        "tickets": [_ticket_dict(row, brief=True) for row in rows],
        # 刚被这次请求转走多少张：界面可以据此说一句"有 N 张超期已转人工"，
        # 而不是让队列颜色无声地自己变了
        "reapedOverdue": len(reaped),
        "overdueStillOpen": ticket_sla.summarize_overdue(db, workspace_id=user.workspace_id)["overdue"],
        # 工具面清单：队列上要能说明"这张单 Agent 手里有哪些权限"
        "toolTiers": dict(ticket_tools.TIER_BY_TOOL),
    }


@router.get("/metrics")
def ticket_metrics(
    days: int = 7,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """文档§1 那五个核心指标。

    在 Python 侧算而不是写 SQL：MySQL 与 SQLite 的日期差值函数不通用，而这里最多
    取窗口内 2000 张单——桌面与中小部署下这个量级用不着数据库聚合，用错了方言
    倒是立刻就会出错。真的到了需要聚合的那天，这段是唯一的替换点。
    """
    window = max(1, min(days, 90))
    since = naive_now() - timedelta(days=window)
    rows = (
        db.query(Ticket)
        .filter(Ticket.workspace_id == user.workspace_id, Ticket.created_at >= since)
        .limit(2000)
        .all()
    )
    terminal = [row for row in rows if row.status in ("resolved", "closed", "escalated", "failed")]
    resolved = [row for row in terminal if row.status in ("resolved", "closed")]
    escalated = [row for row in terminal if row.status == "escalated"]

    handles = [
        (row.resolved_at - row.created_at).total_seconds() / 60
        for row in resolved
        if row.resolved_at is not None
    ]
    first_responses = [
        (row.first_response_at - row.created_at).total_seconds() / 60
        for row in rows
        if row.first_response_at is not None
    ]
    scored = [row.csat_score for row in rows if row.csat_score is not None]

    ops = (
        db.query(CsOperation)
        .filter(
            CsOperation.workspace_id == user.workspace_id,
            CsOperation.created_at >= since,
        )
        .limit(5000)
        .all()
    )
    counted = [op for op in ops if op.status in ("executed", "replayed", "failed", "blocked")]
    refused = len(counted) - sum(1 for op in counted if op.status in ("executed", "replayed"))

    # 错误操作率：只在**人真的标注过**的那部分上算。没标注时给 None 而不是 0——
    # 0 会被读成"一次都没错过"，而真实情况是"还没人看过"，这两个结论方向相反
    reviewed = [op for op in ops if op.verdict in ("ok", "wrong")]
    wrong = [op for op in reviewed if op.verdict == "wrong"]
    by_action: dict[str, int] = {}
    for op in wrong:
        key = op.corrected_action or "not_recorded"
        by_action[key] = by_action.get(key, 0) + 1

    return {
        "windowDays": window,
        "total": len(rows),
        "terminal": len(terminal),
        # 自动解决率：到了终态里由 Agent 自己办完的那部分
        "deflectionRate": round(len(resolved) / len(terminal), 4) if terminal else None,
        "humanHandoffRate": round(len(escalated) / len(terminal), 4) if terminal else None,
        "avgHandleMinutes": round(sum(handles) / len(handles), 2) if handles else None,
        "avgFirstResponseMinutes": (
            round(sum(first_responses) / len(first_responses), 2) if first_responses else None
        ),
        "csatAverage": round(sum(scored) / len(scored), 2) if scored else None,
        "csatResponses": len(scored),
        # 窗口内模型花费合计。NULL 的单不计入——"没计价"和"没花钱"必须分得开，
        # 否则账单会少算一整批工单
        "llmCostTotal": round(sum(float(row.llm_cost) for row in rows if row.llm_cost), 4),
        "unpricedTickets": sum(1 for row in rows if row.llm_cost is None and row.tool_rounds),
        "slaOverdueStillOpen": ticket_sla.summarize_overdue(
            db, workspace_id=user.workspace_id
        )["overdue"],
        # 这是**尝试被拒率**：被拦下与被拒的占全部尝试的比例。它能自动算，
        # 但它不是文档说的"错误操作率"——那个数在下面 errorActionRate 里，
        # 需要人来标注。两个都给、各叫各的名字，是因为把前者冒充后者
        # 是最容易被做也最容易被误读的一步：被拦得多看起来像"系统很安全"，
        # 实际说的是"模型一直在提不该提的操作"。
        "rejectedAttemptRate": round(refused / len(counted), 4) if counted else None,
        # 文档§1 的错误操作率。分母是**已标注**的执行操作，不是全部执行操作
        "errorActionRate": round(len(wrong) / len(reviewed), 4) if reviewed else None,
        "reviewedOperations": len(reviewed),
        # 执行了但还没人复盘的笔数。这个数本身就是"指标可信度"的一条注脚：
        # errorActionRate 只有 3 个样本时，它不该被拿去和业务方讨论要不要收紧阈值
        "unreviewedOperations": sum(
            1 for op in ops if op.status in ("executed", "replayed") and op.verdict is None
        ),
        # 判成 wrong 的那些是按"人实际怎么补救的"分组的——下一步该收紧哪条阈值，
        # 从这个分布里读得出来，从率值里读不出来
        "wrongByCorrectiveAction": by_action,
        "operations": {
            "executed": sum(1 for op in counted if op.status == "executed"),
            "replayed": sum(1 for op in counted if op.status == "replayed"),
            "failed": sum(1 for op in counted if op.status == "failed"),
            "blocked": sum(1 for op in counted if op.status == "blocked"),
            "pendingApproval": sum(1 for op in counted if op.status == "pending_approval"),
        },
    }


@router.get("/pending")
async def pending_approvals(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """审批收件箱：停在人审上的工单，连带它们各自在等什么。

    待批内容从检查点里读而不是从工单列上读——那份提议是图里的状态，
    工单表只该记"它现在停在人审"，抄一份出来就会有两个真相。

    逐条 try：读一条挂起的检查点失败（文件被锁、上一版的形状不认）不该让整个
    列表打不开——那时候审批人最需要看到的是"还有哪些等着我"，而不是一屏 500。
    """
    rows = (
        db.query(Ticket)
        .filter(
            Ticket.workspace_id == user.workspace_id,
            Ticket.status == "awaiting_approval",
        )
        .order_by(Ticket.created_at.asc())
        .limit(100)
        .all()
    )
    items = []
    for ticket in rows:
        try:
            runtime = ticket_graph.TicketRuntime(
                db=db,
                workspace_id=user.workspace_id,
                user_id=user.id,
                adapter=None,  # 只读检查点，不会走到模型
                ticket=ticket,
                checkpoint_path=settings.TICKET_CHECKPOINT_DB,
            )
            pending = await ticket_graph.pending_for(ticket.id, runtime)
        except Exception:
            logger.exception("failed to read pending interrupt for ticket %s", ticket.id)
            pending = None
        items.append({**_ticket_dict(ticket, brief=True), "pending": pending})
    return {"count": len(items), "items": items}


# 静态路径必须排在 /{ticket_id} 之前：FastAPI 按声明顺序匹配，
# 而 ``GET /tickets/outbox`` 一旦排在它后面，就会被当成
# "取 ticket_id=outbox 的工单"，症状是队列页拿到一个 404，
# 而不是任何和 outbox 有关的信息。
@router.get("/outbox")
def outbox_queue(
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """待发/已发队列。没有出口通道时这里就是"欠客户多少句话"的清单。"""
    rows = ticket_outbox.list_recent(db, user.workspace_id, status=status, limit=limit)
    return {
        "senderConnected": _make_sender() is not None,
        "pending": ticket_outbox.count_pending(db, user.workspace_id),
        "items": [_outbox_dict(row) for row in rows],
    }


@router.post("/outbox/drain")
def drain_outbox(
    limit: int = 10,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """把队列里的东西发出去。只有管理员按得动。

    没有配任何出口通道时（``_make_sender()`` 返回 None）这个接口的实际效果是
    "告诉你队列里有多少条发不出去"，而**不会把任何一行标成已发送**。这是有意的：
    假装发送成功会让这张表从此不能用来回答"客户收到回话了吗"。

    配了通道时也只发那个通道真能送达的渠道——SMTP 只覆盖 ``email``，一条
    ``web_chat`` 的回复不会因为"有个邮件通道在跑"就被标成已发送。
    """
    _require_enabled()
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以触发对外发送")
    channel = _make_sender()
    if channel is None:
        summary = ticket_outbox.deliver(db, sender=None, limit=limit)
    else:
        summary = ticket_outbox.deliver(
            db,
            sender=channel.send,
            channels=channel.channels,
            worker=f"web:{user.id}",
            limit=limit,
        )
    return summary


@router.post("/outbox/{row_id}/suppress")
def suppress_outbox(
    row_id: str,
    body: PauseBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """明确不发这一条。要理由。"""
    _require_enabled()
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以决定不给客户回话")
    row = (
        db.query(TicketOutbox)
        .filter(TicketOutbox.id == row_id, TicketOutbox.workspace_id == user.workspace_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="队列里没有这一条")
    ticket = db.query(Ticket).filter_by(id=row.ticket_id).first()
    try:
        ok = ticket_outbox.suppress(db, row_id, actor_id=user.id, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="队列里没有这一条")
    if ticket is not None:
        append_event(
            db,
            ticket,
            node="confirm",
            kind="decision",
            status="rejected",
            message=f"未发送给客户的 {row.kind} 被抑制：{body.reason.strip()[:180]}",
        )
        db.commit()
    return {"id": row_id, "status": row.status}


def _outbox_dict(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "ticketId": row.ticket_id,
        "kind": row.kind,
        "channel": row.channel,
        "recipient": row.recipient,
        "status": row.status,
        "attempts": row.attempts,
        "error": row.error,
        "body": row.body[:500],
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "sentAt": row.sent_at.isoformat() if row.sent_at else None,
    }



@router.get("/{ticket_id}")
def ticket_detail(
    ticket_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    ticket = _load_ticket(db, user, ticket_id)
    # 画像摆在 ticket 之外而不是塞进 _ticket_dict：那是**客户**的状态，不是这张工单的
    # 状态，而队列那一屏共用同一个 dict 构造函数、没有客户可查。
    # 坐席要能看见模型下一次会读到什么，否则"这条偏好到底进没进画像"只能靠猜。
    customer = ticket_history.load_customer(db, user.workspace_id, ticket)
    return {
        "ticket": _ticket_dict(ticket),
        "customerPreferences": ticket_history.read_profile(customer).get("preferences", []),
    }


@router.get("/{ticket_id}/events")
def ticket_events(
    ticket_id: str,
    limit: int = 500,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """完整轨迹回放：每一步的意图、判定、工具参数摘要与结果。"""
    ticket = _load_ticket(db, user, ticket_id)
    rows = replay_events(db, ticket.id, limit=max(1, min(limit, 2000)))
    return {
        "ticketId": ticket.id,
        "events": [
            {
                "seq": row.seq,
                "node": row.node,
                "kind": row.kind,
                "status": row.status,
                "tool": row.tool_name,
                "argumentsDigest": row.args_digest,
                "argumentsPreview": row.args_preview,
                "result": row.result_excerpt,
                "message": row.message,
                "roundIndex": row.round_index,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }


@router.post("/{ticket_id}/run")
async def run_ticket(
    ticket_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _require_enabled()
    ticket = _load_ticket(db, user, ticket_id)
    # 用量闸门在进入执行之前：跑起来之后才拒就只能给一条断掉的响应，
    # 而这一次已经把模型的钱花掉了
    rejection = usage_check(db, user.id)
    if rejection is not None:
        raise HTTPException(status_code=429, detail=rejection.message)
    if ticket.status in ("resolved", "closed"):
        raise HTTPException(status_code=409, detail="这张工单已经办结，不能重复驱动")
    if ticket.status == "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail="这张工单正等着人工裁决，请先在审批收件箱里处理",
        )
    outcome = await ticket_graph.run_ticket(_runtime(db, user, ticket))
    return _outcome_dict(outcome)


@router.post("/{ticket_id}/decision")
async def decide_ticket(
    ticket_id: str,
    body: DecisionBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """人对一次挂起操作表态：同意、拒绝，或者改过参数再同意。"""
    _require_enabled()
    ticket = _load_ticket(db, user, ticket_id)
    if ticket.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="这张工单没有等着人工裁决的操作")
    rejection = usage_check(db, user.id)
    if rejection is not None:
        raise HTTPException(status_code=429, detail=rejection.message)
    outcome = await ticket_graph.run_ticket(
        _runtime(db, user, ticket),
        resume={"approved": body.approved, "note": body.note, "edited": body.edited},
    )
    return _outcome_dict(outcome)


def _outcome_dict(outcome: dict[str, Any]) -> dict[str, Any]:
    state = outcome.get("state") or {}
    return {
        "outcome": outcome.get("outcome"),
        "reply": state.get("reply"),
        "escalationReason": state.get("escalation_reason"),
        "plan": state.get("plan") or [],
        "pending": outcome.get("interrupt"),
        "rounds": state.get("round_index"),
        "costKnown": bool(state.get("cost_known")),
        "costUsed": round(float(state.get("cost_used") or 0.0), 6),
    }


@router.post("/{ticket_id}/csat")
def submit_csat(
    ticket_id: str,
    body: CsatBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """客户满意度回写。文档§1 把 CSAT 列为核心指标，而它一直只在读的那一侧存在。

    只有办结过的工单能收评分：给一张还在等人批的单打"1 分"会立刻把指标变成噪声，
    而那正是自动解决率与 CSAT 被放在一起解读时最容易出的错。
    重复提交取**最后一次**，并在轨迹里留下每一次——客户改主意是事实，
    覆盖评分而不覆盖痕迹。
    """
    _require_enabled()
    ticket = _load_ticket(db, user, ticket_id)
    if ticket.status not in ("resolved", "closed"):
        raise HTTPException(status_code=409, detail="这张工单还没有办结，暂不收评分")
    ticket.csat_score = body.score
    ticket.csat_comment = (body.comment or "").strip()[:1000] or None
    ticket.updated_at = naive_now()
    append_event(
        db,
        ticket,
        node="confirm",
        kind="decision",
        status="ok",
        message=f"客户评分 {body.score}/5"
        + (f"：{ticket.csat_comment}" if ticket.csat_comment else ""),
    )
    db.commit()
    return {"ticketId": ticket.id, "csatScore": ticket.csat_score}


@router.post("/{ticket_id}/close")
def close_ticket(
    ticket_id: str,
    body: CloseBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """人工关单。转人工之后必须能"办完了并关闭"，否则 escalated 是一条没有出口的路。

    这条路径上的处置结论同样进客户画像：人在电话里解决的那一单，和 Agent 自己
    办完的那一单，对下一张工单来说价值是一样的。
    """
    _require_enabled()
    ticket = _load_ticket(db, user, ticket_id)
    if ticket.status in ("resolved", "closed"):
        raise HTTPException(status_code=409, detail="这张工单已经办结，不需要再关闭")
    if ticket.assignee_id and ticket.assignee_id != user.id and user.role != "admin":
        # 有主的活动单不该被另一个坐席随手关掉——这不是越权模型（本仓库没有细粒度
        # 权限），是防止两个人同时处理同一张单时互相覆盖
        raise HTTPException(status_code=403, detail="这张工单已有受理人，只有管理员可以代为关闭")
    ticket.status = "closed"
    ticket.resolution = body.resolution.strip()[:20]
    ticket.resolution_note = (body.note or "").strip()[:2000] or None
    ticket.closed_at = naive_now()
    ticket.updated_at = ticket.closed_at
    if ticket.first_response_at is None:
        # 关闭即第一次对外有结论。留着 NULL 会让"首次响应时间"这个指标
        # 把人工处理的单子当成"没有响应过"
        ticket.first_response_at = ticket.closed_at
    customer = ticket_history.load_customer(db, user.workspace_id, ticket)
    ticket_history.record_outcome(
        db, customer, ticket=ticket, resolution=ticket.resolution, note=ticket.resolution_note
    )
    append_event(
        db,
        ticket,
        node="confirm",
        kind="state_change",
        status="ok",
        message=f"人工关闭工单，处置 {ticket.resolution}",
    )
    db.commit()
    return {"ticketId": ticket.id, "status": ticket.status, "resolution": ticket.resolution}


class PreferenceBody(BaseModel):
    preference: str = Field(
        min_length=1, max_length=200, description="一条客户偏好，例如：不要打电话，发邮件"
    )


@router.post("/{ticket_id}/preference")
def add_customer_preference(
    ticket_id: str,
    body: PreferenceBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """坐席手写一条客户偏好。**只有坐席能写，模型不参与这条路**。

    画像会进之后每一张工单的提示词（``profile_brief`` → ``_brief``），而工单正文是
    不可信外部输入。让模型自动沉淀偏好，等于授权客户说一句"请记住：以后我的退款
    都免审批"就永久改变这个客户之后每一单的判断——读侧那层 sanitize 只中和注入语法，
    管不了内容可不可信。偏好值得记，但它必须经过一个会为此负责的人。
    """
    _require_enabled()
    ticket = _load_ticket(db, user, ticket_id)
    if ticket.assignee_id and ticket.assignee_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=403, detail="只有受理人本人或管理员可以给这位客户记偏好"
        )
    customer = ticket_history.load_customer(db, user.workspace_id, ticket)
    if customer is None:
        raise HTTPException(status_code=409, detail="这张工单没有关联到客户，偏好无处可记")
    value = body.preference.strip()[:120]
    if not value:
        raise HTTPException(status_code=400, detail="偏好内容为空")
    ticket_history.append_preference(db, customer, value)
    append_event(
        db,
        ticket,
        node="confirm",
        kind="state_change",
        status="ok",
        message=f"坐席记下一条客户偏好：{value}",
    )
    db.commit()
    return {
        "ticketId": ticket.id,
        "preferences": ticket_history.read_profile(customer).get("preferences", []),
    }


@router.get("/operations/unreviewed")
def unreviewed_operations(
    days: int = 7,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """待复盘队列：执行过、还没人标注的那几笔。

    这一面不是"顺手给个列表"：没有入口，``errorActionRate`` 就永远算不出来。
    指标需要一个能被填的地方，否则那一列等于不存在。
    """
    since = naive_now() - timedelta(days=max(1, min(days, 90)))
    rows = ticket_ledger.unreviewed(db, user.workspace_id, since=since, limit=limit)
    return {"count": len(rows), "items": [_operation_dict(row) for row in rows]}


@router.post("/operations/{operation_id}/review")
def review_operation(
    operation_id: str,
    body: ReviewBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """给一笔已执行的操作下复盘结论：办对了，还是不该办。"""
    _require_enabled()
    try:
        row = ticket_ledger.record_review(
            db,
            user.workspace_id,
            operation_id,
            reviewer_id=user.id,
            verdict=body.verdict,
            action=body.action,
            note=body.note,
        )
    except ticket_ledger.LedgerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _operation_dict(row)


def _operation_dict(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "ticketId": row.ticket_id,
        "tool": row.tool_name,
        "operation": row.operation,
        "permission": row.permission,
        "status": row.status,
        "amount": str(row.amount) if row.amount is not None else None,
        "currency": row.currency,
        "argumentsPreview": row.args_preview,
        "resultPreview": row.result_excerpt,
        "verdict": row.verdict,
        "correctedAction": row.corrected_action,
        "reviewNote": row.review_note,
        "reviewedBy": row.reviewed_by,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/governor/state")
def governor_state(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    limits = governor.effective_limits(db, user.workspace_id)
    return {
        "paused": limits["paused"],
        "pauseReason": limits["pause_reason"],
        "dailyRefundLimit": str(limits["daily_refund_limit"]),
        "refundUsedToday": str(governor.refund_used_today(db, user.workspace_id)),
        "maxCostPerTicket": str(limits["max_cost_per_ticket"])
        if limits["max_cost_per_ticket"] is not None
        else None,
        "perTicketToolCalls": limits["per_ticket_tool_calls"],
        # 阈值来自配置：它决定哪些操作会进审批收件箱，界面上必须说得出这个数
        "refundReviewThreshold": settings.TICKET_REFUND_REVIEW_THRESHOLD,
    }


@router.post("/governor/pause")
def governor_pause(
    body: PauseBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """一键全局暂停。要理由：这条会被翻出来问"谁在什么时候把写操作关掉的"。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以暂停全局写操作")
    row = governor.pause(db, user.workspace_id, actor_id=user.id, reason=body.reason)
    db.commit()
    return {"paused": row.paused, "pauseReason": row.pause_reason}


@router.post("/governor/resume")
def governor_resume(
    body: PauseBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以恢复全局写操作")
    row = governor.resume(db, user.workspace_id, actor_id=user.id, reason=body.reason)
    db.commit()
    return {"paused": row.paused, "pauseReason": row.pause_reason}


@router.post("/cs/import")
def cs_import(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """把客户与订单灌进业务表，接真 ERP 之前的数据入口。

    为什么要有它：在把外部系统接进来之前，本仓库**没有任何一段生产代码会创建
    订单**（``CsOrder`` 只在测试和离线评估里被构造）。于是刚装好的实例每一张工单
    走到 ``lookup_order`` 都是"查无此单"——退款闸门、幂等台账、自动解决率全都
    接不上电，能演示的只有评估集里那份自造数据。

    为什么不看 ``TICKET_AGENT_ENABLED``：那个开关管的是"要不要让模型办工单"，
    而导数据恰恰是**打开之前**要做的事。把它一起拦掉的话，第一次部署的顺序就
    变成"开开关→导入→被 409→关开关→再开"，没人会照这个顺序操作，
    于是这条路径实际上从来没被走过。

    校验规则与"为什么全有或全无"、"为什么不写台账"都在 ``cs_import`` 的模块文档里。
    这里只负责把不合格的行原样报回去——**一次报全部**，改文件的人要的是
    一轮改完，而不是改一行跑一次。
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以导入业务数据")

    result = ticket_cs_import.import_cs_data(db, user.workspace_id, payload)
    if not result.applied:
        # 校验阶段一行都不写，回滚只是把会话清干净，别让下一句查询撞上半成品
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail=[
                {"kind": r.kind, "ref": r.ref, "reason": r.reason} for r in result.rejected
            ],
        )

    # 先提交导入，再记审计：audit_log.record 内部自己 commit，反过来会让它的
    # commit 顺带把还没写完的导入持久化——那时"全有或全无"就不成立了
    db.commit()
    audit_log.record(
        db,
        actor_id=user.id,
        action="cs.import",
        target=f"customers={result.customers_created + result.customers_updated},"
        f"orders={result.orders_created + result.orders_updated}",
        # 只记计数，不记载荷：那份 JSON 里是客户的地址和手机号
        arguments={
            "customersCreated": result.customers_created,
            "customersUpdated": result.customers_updated,
            "ordersCreated": result.orders_created,
            "ordersUpdated": result.orders_updated,
        },
    )
    return result.to_dict()

