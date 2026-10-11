"""回复出口：入队、租约认领、幂等重试。

配合 ``models.TicketOutbox``——为什么需要这张表、为什么 ``suppressed`` 必须有人明确
选择，见那个模型的文档串。形态照搬 ``services/document_queue``（认领时自增
attempts、租约过期被别人捡走、失败按 attempts 退避），因为要解决的问题一模一样：
一件必须发生、又不能塞进请求里发生的事。

**没有出口通道时 ``deliver`` 什么都不做，也不改任何状态。** 那几行留在 pending 里
就是"欠客户一个回复"的可见证据；把它们悄悄标成 sent 是这一层最坏的一种撒谎——
工单看起来全绿，而客户一句都没收到。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from sqlalchemy import update
from sqlalchemy.orm import Session

from config import settings
from models import Ticket, TicketOutbox
from services.clock import naive_now
from services.ticket.trace import append_event

if TYPE_CHECKING:  # senders 反过来 import 本模块拿 Sender，运行时不能互相 import
    from services.ticket.senders import OutboundChannel

logger = logging.getLogger("ticket.outbox")

KIND_REPLY = "reply"
KIND_CSAT_INVITE = "csat_invite"
KINDS = (KIND_REPLY, KIND_CSAT_INVITE)

# 还没发出去的状态。dedupe 只看这两个：已经 sent 的那一条不能挡住
# "重开之后再回一句"，那是一句新的话
_IN_FLIGHT = ("pending", "sending")

# 一轮认领最多试几条：高并发下总有人抢先，但一直撞说明不该再占着这个 worker 了
_CLAIM_ATTEMPTS = 5

# sender 的签名：(channel, recipient, body) -> None，抛异常即失败
Sender = Callable[[str, str | None, str], None]


@dataclass
class Claimed:
    id: str
    ticket_id: str
    channel: str
    recipient: str | None
    body: str
    attempts: int


def enqueue(
    db: Session,
    ticket: Ticket,
    *,
    kind: str,
    body: str,
    recipient: str | None = None,
) -> str | None:
    """把一条要发给客户的话排进队列。返回行 id；被去重或写失败时返回 None。

    入队失败**必须留下痕迹**：这一条丢了就等于客户永远收不到回话，而工单状态
    看起来完全正常。所以除了日志，还在轨迹里写一条——回放时能看到"办完了但没发出去"，
    这是这个缺陷唯一能被发现的时刻。
    """
    if kind not in KINDS:
        raise ValueError(f"未知的出口类型：{kind!r}，可选：{list(KINDS)}")
    if not body.strip():
        return None
    try:
        existing = (
            db.query(TicketOutbox.id)
            .filter(
                TicketOutbox.ticket_id == ticket.id,
                TicketOutbox.kind == kind,
                TicketOutbox.status.in_(_IN_FLIGHT),
            )
            .first()
        )
        if existing is not None:
            return None
        now = naive_now()
        row = TicketOutbox(
            id=str(uuid.uuid4()),
            workspace_id=ticket.workspace_id,
            ticket_id=ticket.id,
            kind=kind,
            channel=ticket.channel,
            recipient=recipient,
            body=body[:8000],
            status="pending",
            attempts=0,
            max_attempts=max(1, settings.TICKET_OUTBOX_MAX_ATTEMPTS),
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.commit()
        return row.id
    except Exception:
        db.rollback()
        logger.exception("failed to enqueue %s for ticket %s", kind, ticket.id)
        try:
            append_event(
                db,
                ticket,
                node="confirm",
                kind="error",
                status="blocked",
                message=f"要给客户发的那条 {kind} 没能排进发送队列，这句话目前没有人发得出",
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("failed to trace the enqueue failure")
        return None


def reap_expired_leases(db: Session, limit: int = 100) -> int:
    """发送中途进程死掉留下的 sending 行推回 pending。

    挂在读路径上，和 ``document_queue`` 同一形状：本仓库没有调度器。
    """
    now = naive_now()
    try:
        rows = (
            db.query(TicketOutbox)
            .filter(
                TicketOutbox.status == "sending",
                TicketOutbox.lease_expires_at.is_not(None),
                TicketOutbox.lease_expires_at <= now,
            )
            .limit(max(1, min(limit, 500)))
            .all()
        )
        for row in rows:
            row.status = "pending"
            row.lease_owner = None
            row.lease_expires_at = None
            row.error = "发送租约过期（进程在发送中途没了）"
            row.updated_at = now
        if rows:
            db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        logger.exception("reap_expired_leases failed")
        return 0


def claim_next(
    db: Session, worker_id: str, channels: frozenset[str] | None = None
) -> Claimed | None:
    """认领一条待发送。认领即占租约并自增 attempts。

    ``channels`` 是**这个 worker 真能送达的渠道**。不给就什么都领（手动排空那种
    "有什么发什么"的语义）；给了就把别的渠道留在队列里继续 pending。理由和
    ``deliver`` 不许假装成功是同一个：用邮件通道把一条 ``web_chat`` 的回复标成 sent，
    等于往台账里写了一次不存在的送达。

    原子性靠**带谓词的条件 UPDATE**，不是 ``SELECT ... FOR UPDATE SKIP LOCKED``：
    "把这一行从 pending 改成 sending"本身就是一次写，改动人就是赢家；``rowcount``
    为 0 说明被别人抢先了，换下一条再试。这段 SQL 在 MySQL 和 SQLite 上是同一个意思。

    原来那一版是 SKIP LOCKED 外加一条"方言不支持就退回普通取"的兜底，而那条兜底
    恰好只在 SQLite 上生效：两个进程各查各的、认领到同一行，于是**同一封邮件发两遍**。
    今天没爆只是因为还没有真实的发送通道——所以这一步必须在自动排空之前先修掉。
    """
    now = naive_now()
    lease_seconds = max(1, int(settings.TICKET_OUTBOX_LEASE_SECONDS))

    for _ in range(_CLAIM_ATTEMPTS):
        query = db.query(TicketOutbox.id).filter(
            TicketOutbox.status == "pending", TicketOutbox.available_at <= now
        )
        if channels:
            query = query.filter(TicketOutbox.channel.in_(tuple(sorted(channels))))
        candidate = query.order_by(TicketOutbox.available_at.asc()).limit(1).first()
        if candidate is None:
            return None
        row_id = candidate[0]
        result = db.execute(
            update(TicketOutbox)
            .where(TicketOutbox.id == row_id, TicketOutbox.status == "pending")
            .values(
                status="sending",
                lease_owner=worker_id[:64],
                lease_expires_at=now + timedelta_seconds(lease_seconds),
                attempts=TicketOutbox.attempts + 1,
                updated_at=now,
            )
        )
        db.commit()
        if result.rowcount == 0:
            # 被别人抢先。它现在已经是 sending 了，所以下一次 SELECT 不会又捞到同一行
            continue
        row = db.get(TicketOutbox, row_id)
        return Claimed(
            id=row.id,
            ticket_id=row.ticket_id,
            channel=row.channel,
            recipient=row.recipient,
            body=row.body,
            attempts=row.attempts,
        )
    return None


def timedelta_seconds(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def mark_sent(db: Session, row_id: str) -> None:
    now = naive_now()
    row = db.get(TicketOutbox, row_id)
    if row is None:
        return
    row.status = "sent"
    row.sent_at = now
    row.updated_at = now
    row.lease_owner = None
    row.lease_expires_at = None
    row.error = None
    db.commit()


def fail_with_retry(db: Session, row_id: str, error: str = "") -> bool:
    """失败。没到尝试上限就退避重排（返回 True），到了就落 failed（返回 False）。

    退避把 ``available_at`` 推到 ``now + 基数 × attempts``：一条一直发不出去的邮件
    不应该每被 drain 一次就再打服务商一次。
    """
    now = naive_now()
    row = db.get(TicketOutbox, row_id)
    if row is None:
        return False
    row.updated_at = now
    row.lease_owner = None
    row.lease_expires_at = None
    row.error = error[:200] or "发送失败"
    if row.attempts >= row.max_attempts:
        row.status = "failed"
        db.commit()
        return False
    row.status = "pending"
    row.available_at = now + timedelta_seconds(
        settings.TICKET_OUTBOX_BACKOFF_SECONDS * row.attempts
    )
    db.commit()
    return True


def suppress(db: Session, row_id: str, *, actor_id: str, reason: str) -> bool:
    """显式不发。**要理由，而且要留痕。**

    被抑制的消息不会再自己发出去，所以这个状态只能由一个对此负责的人给出：
    "客户拒收营销信息"是真的，"刚才发送报错了先标一下"不是——后者属于
    ``fail_with_retry``，它会自己再试，也不会假装这件事已经解决。
    """
    if not reason.strip():
        raise ValueError("抑制一条待发消息必须写明理由")
    row = db.get(TicketOutbox, row_id)
    if row is None:
        return False
    row.status = "suppressed"
    row.error = f"由 {actor_id} 抑制：{reason.strip()[:180]}"
    row.updated_at = naive_now()
    row.lease_owner = None
    row.lease_expires_at = None
    db.commit()
    return True


def deliver(
    db: Session,
    *,
    sender: Sender | None,
    worker: str = "ticket-outbox",
    limit: int = 10,
    channels: frozenset[str] | None = None,
) -> dict[str, Any]:
    """把队列里的东西发出去。返回一份发生了几次什么的汇总。

    ``sender is None`` 时直接返回、**一行状态都不改**：没有出口通道不是"发送失败"，
    更不是"发送成功"。把 pending 悄悄刷成 sent 会让这张表从此不能再用来回答
    "客户收到回话了吗"，而它正是为这个问题存在的。

    ``channels`` 见 ``claim_next``：这一批只领这个通道真能送达的渠道。
    """
    if sender is None:
        pending = count_pending(db)
        return {
            "sent": 0,
            "failed": 0,
            "pending": pending,
            "delivered": False,
            "reason": "没有接入任何发送通道（邮件/企微/短信），待发内容仍留在队列里",
        }
    reap_expired_leases(db)
    sent = failed = 0
    for _ in range(max(1, min(limit, 100))):
        claimed = claim_next(db, worker, channels)
        if claimed is None:
            break
        try:
            sender(claimed.channel, claimed.recipient, claimed.body)
        except Exception as exc:
            retried = fail_with_retry(db, claimed.id, f"{type(exc).__name__}: {exc}")
            if not retried:
                failed += 1
            continue
        mark_sent(db, claimed.id)
        sent += 1
    return {
        "sent": sent,
        "failed": failed,
        "pending": count_pending(db),
        "delivered": True,
        "reason": "",
    }


def count_pending(db: Session, workspace_id: str | None = None) -> int:
    query = db.query(TicketOutbox.id).filter(TicketOutbox.status == "pending")
    if workspace_id:
        query = query.filter(TicketOutbox.workspace_id == workspace_id)
    return query.count()


def list_recent(
    db: Session, workspace_id: str, *, status: str | None = None, limit: int = 50
) -> list[TicketOutbox]:
    query = db.query(TicketOutbox).filter(TicketOutbox.workspace_id == workspace_id)
    if status:
        query = query.filter(TicketOutbox.status == status)
    return (
        query.order_by(TicketOutbox.created_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )


CSAT_INVITE_BODY = "这张工单已经处理完了。方便的话给我们打个分（1-5 分），您的评价会直接影响我们决定下一步收紧哪条规则。"


# ========== 自动排空 ==========


async def drain_once(
    *, channel: OutboundChannel, worker: str = "ticket-outbox"
) -> dict[str, Any]:
    """排空一批（一个会话、一次认领一轮）。给循环和启动重驱用。"""
    from database import SessionLocal

    db = SessionLocal()
    try:
        return deliver(
            db,
            sender=channel.send,
            worker=worker,
            channels=channel.channels,
            limit=settings.TICKET_OUTBOX_DRAIN_BATCH,
        )
    finally:
        db.close()


async def drain_pass(*, worker: str = "ticket-outbox") -> dict[str, Any] | None:
    """醒一次：有出口通道就排空一批，没有就返回 None 并且什么都不改。

    单独一个函数而不是写在循环里：循环要睡觉，而"没有通道时不许动状态"这条
    判据不该只能在真等过 ``asyncio.sleep`` 之后才测得到。
    """
    from services.ticket import senders

    channel = senders.build_sender()
    if channel is None:
        return None
    return await drain_once(channel=channel, worker=worker)


async def run_autodrain() -> None:
    """周期性排空待发队列。开关与间隔见 ``settings.TICKET_OUTBOX_AUTODRAIN``。

    ## 为什么要有这个循环

    在这之前 ``deliver`` 全仓库只有一个调用方：治理台上管理员手动点的那一下。于是
    "办结的回复什么时候发出去"取决于有没有人恰好去点——不点就只会变长。工单办完了、
    答复生成了、客户一个字都没收到，而队列页面上每一行都是 pending。

    ## 为什么多实例可以一起跑

    认领走带 ``status='pending'`` 谓词的条件 UPDATE（见 ``claim_next``），两个进程
    拿到同一行的那一个会拿到 ``rowcount == 0``，换下一条。所以每个实例各醒一次只是
    多查一趟，不会把同一句话发两遍。

    ## 没有通道时它什么都不做，但要说一次

    ``build_sender()`` 返回 None 是当前的实际状态。循环每 N 秒醒来、发现没有出口、
    再睡回去——那不该产生任何写入，也不该刷日志，所以只在**第一次**发现时警告一句。
    """
    import asyncio

    worker = f"autodrain:{uuid.uuid4().hex[:8]}"
    interval = max(5, int(settings.TICKET_OUTBOX_DRAIN_INTERVAL_SECONDS))
    warned_no_sender = False

    while True:
        await asyncio.sleep(interval)
        try:
            summary = await drain_pass(worker=worker)
        except Exception:  # noqa: BLE001 - 循环不能被一次失败带走
            logger.exception("outbox autodrain pass failed")
            continue
        if summary is None:
            if not warned_no_sender:
                logger.warning(
                    "TICKET_OUTBOX_AUTODRAIN 打开了，但 senders.build_sender() 仍返回 "
                    "None —— 没有出口通道，待发队列不会被发送。要么接一种通道，要么"
                    "把这个开关关掉。"
                )
                warned_no_sender = True
            continue
        if warned_no_sender:
            logger.info("发送通道已就位，自动排空恢复工作")
            warned_no_sender = False
        if summary["sent"] or summary["failed"]:
            logger.info(
                "outbox autodrain: sent=%s failed=%s pending=%s",
                summary["sent"],
                summary["failed"],
                summary["pending"],
            )
