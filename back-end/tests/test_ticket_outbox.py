"""回复出口：入队去重、租约重试、显式抑制，以及"没通道时什么都不改"。

最后一条是这个文件里最要紧的断言。没有出口通道时把 pending 标成 sent，是让这张表
从此再也回答不了"客户收到回话了吗"——而它是这张表存在的唯一理由。
"""
from datetime import timedelta

import pytest

from config import settings
from models import Ticket, TicketOutbox
from services.clock import naive_now
from services.ticket import outbox


@pytest.fixture(autouse=True)
def _ticket_settings(monkeypatch):
    monkeypatch.setattr(settings, "TICKET_OUTBOX_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "TICKET_OUTBOX_LEASE_SECONDS", 120)
    monkeypatch.setattr(settings, "TICKET_OUTBOX_BACKOFF_SECONDS", 60)


def _ticket(db, *, ticket_id="t1", channel="email", workspace_id="w1") -> Ticket:
    now = naive_now()
    ticket = Ticket(
        id=ticket_id, workspace_id=workspace_id, channel=channel, status="resolved",
        request_text="ORD-1 帮我查查", created_at=now, updated_at=now,
    )
    db.add(ticket)
    db.commit()
    return ticket


def test_办结的回话与邀评各排一条(db_real):
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="您的包裹明天到", recipient="z@corp.com")
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_CSAT_INVITE, body=outbox.CSAT_INVITE_BODY, recipient="z@corp.com")
    rows = db_real.query(TicketOutbox).all()
    assert {row.kind for row in rows} == {"reply", "csat_invite"}
    assert all(row.status == "pending" for row in rows)
    assert outbox.count_pending(db_real, "w1") == 2


def test_同一张单同一种消息不重复排队(db_real):
    """重跑一次确认节点不该让客户收到两封一样的邮件。"""
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="第一遍")
    again = outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="第二遍")
    assert again is None
    assert db_real.query(TicketOutbox).filter_by(kind="reply").count() == 1


def test_空正文不排队(db_real):
    ticket = _ticket(db_real)
    assert outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="   ") is None


def test_未知类型直接报错而不是静默入库(db_real):
    ticket = _ticket(db_real)
    with pytest.raises(ValueError):
        outbox.enqueue(db_real, ticket, kind="sms_spam", body="x")


def test_没有出口通道时deliver一行状态都不改(db_real):
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="您的包裹明天到")
    summary = outbox.deliver(db_real, sender=None)
    assert summary["delivered"] is False and summary["pending"] == 1
    assert db_real.query(TicketOutbox).filter_by(status="pending").count() == 1
    assert db_real.query(TicketOutbox).filter_by(status="sent").count() == 0


def test_接上通道后真的发出去并留下时间戳(db_real):
    ticket = _ticket(db_real)
    row_id = outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到", recipient="z@corp.com")
    sent: list[tuple] = []
    summary = outbox.deliver(db_real, sender=lambda channel, recipient, body: sent.append((channel, recipient, body)))
    assert summary["sent"] == 1 and summary["pending"] == 0
    assert sent == [("email", "z@corp.com", "明天到")]
    row = db_real.query(TicketOutbox).filter_by(id=row_id).one()
    assert row.status == "sent" and row.sent_at is not None and row.attempts == 1


def test_发送失败按尝试次数退避重排(db_real):
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")

    def boom(channel, recipient, body):
        raise RuntimeError("smtp 550")

    summary = outbox.deliver(db_real, sender=boom)
    row = db_real.query(TicketOutbox).one()
    assert row.status == "pending" and row.attempts == 1
    assert "smtp 550" in row.error
    # available_at 被推到未来：一条一直发不出去的消息不该每次 drain 都再打服务商一次
    assert row.available_at > naive_now() + timedelta(seconds=30)
    assert summary["sent"] == 0


def test_试满上限才落成failed(db_real):
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")

    def boom(channel, recipient, body):
        raise RuntimeError("smtp 550")

    for _ in range(3):
        # 退避到未来的行要能立刻再被认领，才能测到上限这条路径
        row = db_real.query(TicketOutbox).one()
        row.available_at = naive_now()
        db_real.commit()
        outbox.deliver(db_real, sender=boom)
    assert db_real.query(TicketOutbox).one().status == "failed"
    assert db_real.query(TicketOutbox).one().attempts == 3


def test_发送中途进程没掉时租约过期会被捡回(db_real):
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")
    claimed = outbox.claim_next(db_real, "dead-worker")
    assert claimed is not None
    row = db_real.query(TicketOutbox).one()
    row.lease_expires_at = naive_now() - timedelta(seconds=1)
    db_real.commit()
    assert outbox.reap_expired_leases(db_real) == 1
    assert db_real.query(TicketOutbox).one().status == "pending"


def test_抑制必须给理由而且要显式(db_real):
    ticket = _ticket(db_real)
    row_id = outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")
    with pytest.raises(ValueError):
        outbox.suppress(db_real, row_id, actor_id="u1", reason="  ")
    assert outbox.suppress(db_real, row_id, actor_id="u1", reason="客户已电话解决，别再发邮件")
    row = db_real.query(TicketOutbox).one()
    assert row.status == "suppressed" and "客户已电话解决" in row.error


def test_被抑制的不会再被认领出去(db_real):
    ticket = _ticket(db_real)
    row_id = outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")
    outbox.suppress(db_real, row_id, actor_id="u1", reason="已电话沟通")
    assert outbox.claim_next(db_real, "w") is None


# ========== 认领的原子性（自动排空的前置） ==========


def test_认领发出的UPDATE必须带着status谓词(db_real):
    """钉的是**语句形状**，不是行为。这条是上面那两条测不到的东西。

    为什么只能这么测：旧写法（``FOR UPDATE SKIP LOCKED`` + "方言不支持就退回普通取"）
    真正的失效要两个进程在"SELECT 挑中这行"与"UPDATE 改成 sending"之间交错。那个窗口
    在单会话里复现不出来——把两次 ``claim_next`` 连着跑，新旧两版都会返回 None，于是
    测试全绿而 bug 还在，客户照样收到两遍一样的邮件。

    但两种写法在**语句形状**上现在就不一样：按主键改的 ORM 写法 WHERE 里只有 id，
    条件写的 WHERE 里还有一句 ``status = 'pending'``。前者在并发下会双发，后者不会。
    所以这里去听引擎实际发出的 SQL。
    """
    from sqlalchemy import event

    captured: list[str] = []

    def _spy(conn, cursor, statement, parameters, context, executemany):
        if "UPDATE ticket_outbox" in statement:
            captured.append(statement)

    engine = db_real.get_bind()
    event.listen(engine, "before_cursor_execute", _spy)
    try:
        ticket = _ticket(db_real)
        outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")
        assert outbox.claim_next(db_real, "worker-spy") is not None
    finally:
        event.remove(engine, "before_cursor_execute", _spy)

    assert captured, "认领没有发出任何 UPDATE"
    where = captured[-1].split("WHERE", 1)[-1]
    assert "status" in where, f"认领的 UPDATE 没带 status 谓词，并发下会双发：{where}"


def test_行不再是pending时认领不动它(db_real):
    """条件写的可观察后果：抢不到的人空手而归，也不留下第二次 attempts。"""
    from sqlalchemy import update as sa_update

    ticket = _ticket(db_real)
    row_id = outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")
    # 模拟"另一个进程在我们之前把它拿走了"
    db_real.execute(
        sa_update(TicketOutbox).where(TicketOutbox.id == row_id).values(status="sending")
    )
    db_real.commit()

    assert outbox.claim_next(db_real, "worker-late") is None
    assert db_real.get(TicketOutbox, row_id).attempts == 0, "抢输的不该把 attempts 加上去"


def test_一轮里发出去的每条都只被发一次(db_real):
    """端到端：deliver 拿到的 sender 调用次数必须等于行数，不多不少。"""
    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="甲")
    other = _ticket(db_real, ticket_id="t2")
    outbox.enqueue(db_real, other, kind=outbox.KIND_REPLY, body="乙")

    sent: list[str] = []
    summary = outbox.deliver(db_real, sender=lambda c, r, b: sent.append(b), limit=10)

    assert set(sent) == {"甲", "乙"} and len(sent) == 2
    assert summary["sent"] == 2 and summary["pending"] == 0
    assert db_real.query(TicketOutbox).filter_by(status="sent").count() == 2


# ========== 自动排空那一次"醒" ==========


def test_没有出口通道时排空一次什么都不改(db_real, monkeypatch):
    """``drain_pass`` 是循环里可测的那一半：不测它就得真等一次 sleep 才敢保证它不撒谎。"""
    import asyncio

    from services.ticket import senders

    ticket = _ticket(db_real)
    outbox.enqueue(db_real, ticket, kind=outbox.KIND_REPLY, body="明天到")
    monkeypatch.setattr(senders, "build_sender", lambda: None)

    assert asyncio.run(outbox.drain_pass(worker="w-test")) is None
    assert db_real.query(TicketOutbox).filter_by(status="pending").count() == 1


def test_开关决定循环起不起而关掉时留得干净(db_real, monkeypatch):
    """生命周期本身。

    起不来的后果是"待发队列永远没人排"，而启动那行是唯一能看见它到底起了没有的地方；
    停不干净的后果是每次测试跑完多一条 "Task was destroyed but it is pending"，
    那种噪声会让人开始忽略警告——而 shutdown 里那三行存在的唯一理由就是它。
    """
    import main
    from database import get_db
    from fastapi.testclient import TestClient

    def _override_db():
        yield db_real

    def _boot():
        main.app.dependency_overrides[get_db] = _override_db
        main.app.state.limiter.enabled = False
        try:
            with TestClient(main.app) as client:
                return getattr(client.app.state, "outbox_drain", "没有这个字段")
        finally:
            main.app.state.limiter.enabled = True
            main.app.dependency_overrides.clear()

    monkeypatch.setattr(settings, "TICKET_AGENT_ENABLED", True)
    monkeypatch.setattr(settings, "TICKET_OUTBOX_DRAIN_INTERVAL_SECONDS", 5)

    monkeypatch.setattr(settings, "TICKET_OUTBOX_AUTODRAIN", False)
    assert _boot() is None, "开关关着却起了循环"

    monkeypatch.setattr(settings, "TICKET_OUTBOX_AUTODRAIN", True)
    task = _boot()
    assert hasattr(task, "cancel"), f"开关开着应当起了循环，拿到的是 {task!r}"
    assert task.done(), "with 退出后循环应当已被 shutdown 取消，而不是留一个悬着的任务"
