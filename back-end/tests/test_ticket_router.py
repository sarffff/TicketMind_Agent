"""工单接口：真实 ASGI 链路 + JWT。

service 层的单测证明了各层自己的行为，这一层证明的是**接线**：路由有没有把
workspace 取对、挂起与裁决能不能跨请求接上、开关关掉之后界面拿到的是 409 而不是
一屏空白，以及别人的工单是不是真的读不到——这些只有走完整链路才测得到。
"""
import json

import pytest
import sqlalchemy

from config import settings
from conftest import ScriptedAdapter

PASSWORD = "Passw0rd123"
@pytest.fixture()
def db_session():
    from database import Base

    import models  # noqa: F401

    engine = sqlalchemy.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session = sqlalchemy.orm.sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from main import app
    from database import get_db
    from routers import ticket_router

    def _override_db():
        yield db_session

    monkeypatch.setattr(settings, "TICKET_AGENT_ENABLED", True)
    monkeypatch.setattr(settings, "TICKET_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setattr(settings, "TICKET_UNDERSTAND_LLM", True)
    monkeypatch.setattr(settings, "TICKET_REFUND_REVIEW_THRESHOLD", 200.0)
    monkeypatch.setattr(settings, "STRUCTURED_OUTPUT_RETRIES", 0)
    monkeypatch.setattr(settings, "TICKET_CHANNELS", "web_chat,email,app,api")

    app.dependency_overrides[get_db] = _override_db
    app.state.limiter.enabled = False
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.state.limiter.enabled = True
        app.dependency_overrides.clear()


def _headers(client, *, email="alice@example.com", username="alice"):
    client.post("/auth/register", json={"email": email, "username": username, "password": PASSWORD})
    token = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _script(client, monkeypatch, rounds_per_request):
    """按请求顺序发适配器：run 那一次要几轮，decision 恢复时再要几轮。

    每个请求的脚本都在第一轮（理解层）之后自动补一轮规划——图里 understand → plan
    → 执行循环，测试脚本里逐条写一遍"[]"只是噪声。
    """
    from routers import ticket_router

    queue = []
    for rounds in rounds_per_request:
        script = list(rounds)
        if len(script) > 1:
            script.insert(1, {"text": "[]"})
        queue.append(script)

    def _make():
        return ScriptedAdapter(queue.pop(0) if queue else [])

    monkeypatch.setattr(ticket_router, "_make_adapter", _make)


def _seed_order(db_session, user):
    from models import CsCustomer, CsOrder
    from services.clock import naive_now

    now = naive_now()
    db_session.add(
        CsCustomer(
            id="cust1", workspace_id=user.workspace_id, display_name="张三",
            email="zhang@corp.com", created_at=now, updated_at=now,
        )
    )
    db_session.add(
        CsOrder(
            workspace_id=user.workspace_id, customer_id="cust1",
            order_no="ORD20260115001", status="paid", total_amount="350.00",
            refunded_amount="0.00", receiver_name="张三", receiver_phone="13800138000",
            address_text="北京市海淀区中关村大街1号", created_at=now, updated_at=now,
        )
    )
    db_session.commit()


def _understand(intent, amount=""):
    return {
        "text": json.dumps(
            {
                "intent": intent,
                "product": "耳机",
                "sentiment": "calm",
                "order_nos": ["ORD20260115001"],
                "amount": amount,
                "summary": "客户要办事",
            },
            ensure_ascii=False,
        )
    }


# ========== 开关 ==========


def test_开关关掉时提交被拒而队列仍然读得出来(client, monkeypatch):
    monkeypatch.setattr(settings, "TICKET_AGENT_ENABLED", False)
    headers = _headers(client)
    rejected = client.post("/tickets", json={"content": "我的订单还没到"}, headers=headers)
    assert rejected.status_code == 409
    body = client.get("/tickets", headers=headers).json()
    # 前端要能区分"没开这个能力"和"开了但一条都没有"，否则用户以为是自己点错了
    assert body["enabled"] is False
    assert body["tickets"] == []


def test_未登录碰不到工单(client):
    assert client.get("/tickets").status_code in (401, 403)
    assert client.post("/tickets", json={"content": "x"}).status_code in (401, 403)


# ========== 提交 ==========


def test_提交建单并进队列(client, db_session):
    headers = _headers(client)
    created = client.post(
        "/tickets",
        json={
            "channel": "email",
            "content": "订单 ORD20260115001 还没发货\n\n> 原始邮件\n> 上次那单已取消",
            "customer_email": "Zhang@Corp.com",
            "external_ref": "<msg-1@mail>",
        },
        headers=headers,
    ).json()
    assert created["created"] is True
    listing = client.get("/tickets", headers=headers).json()
    assert listing["total"] == 1
    assert listing["tickets"][0]["id"] == created["ticketId"]
    assert listing["tickets"][0]["channel"] == "email"

    detail = client.get(f"/tickets/{created['ticketId']}", headers=headers).json()["ticket"]
    # 引述被清洗掉了：那封信里剩下的只有本次诉求
    assert "原始邮件" not in detail["requestText"]
    assert detail["entities"]["identity"] == {"email": "zhang@corp.com"}

    events = client.get(f"/tickets/{created['ticketId']}/events", headers=headers).json()["events"]
    assert [event["node"] for event in events] == ["intake"]


def test_同一封邮件重复投递不会建出第二张单(client):
    headers = _headers(client)
    payload = {"channel": "email", "content": "要退款", "external_ref": "<dup@mail>"}
    first = client.post("/tickets", json=payload, headers=headers).json()
    again = client.post("/tickets", json=payload, headers=headers).json()
    assert again["created"] is False
    assert again["ticketId"] == first["ticketId"]
    assert again["dedupedAgainst"] == first["ticketId"]
    assert client.get("/tickets", headers=headers).json()["total"] == 1


def test_正文超限是400而不是悄悄建一张缺订单号的单(client, monkeypatch):
    monkeypatch.setattr(settings, "TICKET_INTAKE_MAX_CHARS", 20)
    headers = _headers(client)
    response = client.post("/tickets", json={"content": "订" * 40}, headers=headers)
    assert response.status_code == 400
    assert "超过上限" in response.json()["detail"]


def test_未知渠道被拒并列出可选值(client):
    headers = _headers(client)
    response = client.post("/tickets", json={"channel": "fax", "content": "要退款"}, headers=headers)
    assert response.status_code == 400
    assert "未知渠道" in response.json()["detail"]


# ========== 执行与裁决 ==========


def test_查询单一路接口跑完(client, db_session, monkeypatch):
    headers = _headers(client)
    ticket_id = client.post(
        "/tickets", json={"content": "订单 ORD20260115001 还没发货，帮我查"}, headers=headers
    ).json()["ticketId"]
    _seed_order(db_session, _current_user(client, db_session))
    _script(
        client,
        monkeypatch,
        [
            [
                _understand("query_logistics"),
                {"tool_calls": [("lookup_order", {"order_no": "ORD20260115001"})]},
                {"text": "您这单还没发货。"},
            ]
        ],
    )
    outcome = client.post(f"/tickets/{ticket_id}/run", headers=headers).json()
    assert outcome["outcome"] == "resolved"
    assert outcome["reply"] == "您这单还没发货。"
    detail = client.get(f"/tickets/{ticket_id}", headers=headers).json()["ticket"]
    assert detail["status"] == "resolved"
    assert detail["riskLevel"] == "low"


def test_退款单挂起收件箱看得到批准后才落库(client, db_session, monkeypatch):
    headers = _headers(client)
    ticket_id = client.post(
        "/tickets", json={"content": "订单 ORD20260115001 要退款 350 元"}, headers=headers
    ).json()["ticketId"]
    _seed_order(db_session, _current_user(client, db_session))
    _script(
        client,
        monkeypatch,
        [
            [
                _understand("refund", amount="350"),
                {"tool_calls": [("create_refund", {"order_no": "ORD20260115001", "amount": "350"})]},
            ],
            [{"text": "退款已提交，请留意到账。"}],
        ],
    )

    run = client.post(f"/tickets/{ticket_id}/run", headers=headers).json()
    assert run["outcome"] == "awaiting_approval"
    assert run["pending"]["calls"][0]["name"] == "create_refund"
    from models import CsRefund

    assert db_session.query(CsRefund).count() == 0

    inbox = client.get("/tickets/pending", headers=headers).json()
    assert inbox["count"] == 1
    assert inbox["items"][0]["id"] == ticket_id
    assert inbox["items"][0]["pending"]["calls"][0]["arguments"].startswith("{")

    # 没挂起的工单不接受裁决
    other = client.post("/tickets", json={"content": "查下物流"}, headers=headers).json()["ticketId"]
    assert client.post(
        f"/tickets/{other}/decision", json={"approved": True}, headers=headers
    ).status_code == 409

    decided = client.post(
        f"/tickets/{ticket_id}/decision",
        json={"approved": True, "note": "同意退款"},
        headers=headers,
    ).json()
    assert decided["outcome"] == "resolved"
    assert db_session.query(CsRefund).count() == 1


def test_改过参数再同意执行的是改过的那份(client, db_session, monkeypatch):
    from models import CsRefund

    headers = _headers(client)
    ticket_id = client.post(
        "/tickets", json={"content": "订单 ORD20260115001 要退款 350 元"}, headers=headers
    ).json()["ticketId"]
    _seed_order(db_session, _current_user(client, db_session))
    _script(
        client,
        monkeypatch,
        [
            [
                _understand("refund", amount="350"),
                {"tool_calls": [("create_refund", {"order_no": "ORD20260115001", "amount": "350"})]},
            ],
            [{"text": "按协商金额退了。"}],
        ],
    )
    client.post(f"/tickets/{ticket_id}/run", headers=headers)
    decided = client.post(
        f"/tickets/{ticket_id}/decision",
        json={"approved": True, "note": "只退一半", "edited": {"call-0": {"amount": "200"}}},
        headers=headers,
    ).json()
    assert decided["outcome"] == "resolved"
    assert str(db_session.query(CsRefund).one().amount).startswith("200")


def test_拒绝之后转人工而不是自行收尾(client, db_session, monkeypatch):
    from models import CsRefund

    headers = _headers(client)
    ticket_id = client.post(
        "/tickets", json={"content": "订单 ORD20260115001 要退款 350 元"}, headers=headers
    ).json()["ticketId"]
    _seed_order(db_session, _current_user(client, db_session))
    _script(
        client,
        monkeypatch,
        [
            [
                _understand("refund", amount="350"),
                {"tool_calls": [("create_refund", {"order_no": "ORD20260115001", "amount": "350"})]},
            ],
            [{"text": "这笔需要专人再核。"}],
        ],
    )
    client.post(f"/tickets/{ticket_id}/run", headers=headers)
    decided = client.post(
        f"/tickets/{ticket_id}/decision",
        json={"approved": False, "note": "缺少质量凭证"},
        headers=headers,
    ).json()
    assert decided["outcome"] == "escalated"
    assert decided["escalationReason"] == "approval_rejected"
    assert db_session.query(CsRefund).count() == 0
    detail = client.get(f"/tickets/{ticket_id}", headers=headers).json()["ticket"]
    assert detail["status"] == "escalated"


def test_已经办结的工单不接受第二次驱动(client, db_session, monkeypatch):
    headers = _headers(client)
    ticket_id = client.post("/tickets", json={"content": "查下 ORD20260115001"}, headers=headers).json()["ticketId"]
    _seed_order(db_session, _current_user(client, db_session))
    _script(
        client,
        monkeypatch,
        [
            [
                _understand("query_order"),
                {"tool_calls": [("lookup_order", {"order_no": "ORD20260115001"})]},
                {"text": "已发货。"},
            ]
        ],
    )
    assert client.post(f"/tickets/{ticket_id}/run", headers=headers).json()["outcome"] == "resolved"
    assert client.post(f"/tickets/{ticket_id}/run", headers=headers).status_code == 409


# ========== 治理与越权 ==========


def test_暂停只有管理员能按(client, db_session):
    headers = _headers(client)
    user = _current_user(client, db_session)
    # 注册出来的第一个用户默认就是 admin（见 User.role 的默认值），
    # 要验"普通坐席按不动暂停键"得先把他降下来
    user.role = "user"
    db_session.commit()

    plain = client.post("/tickets/governor/pause", json={"reason": "演练"}, headers=headers)
    assert plain.status_code == 403

    user.role = "admin"
    db_session.commit()
    paused = client.post("/tickets/governor/pause", json={"reason": "支付网关故障"}, headers=headers)
    assert paused.status_code == 200 and paused.json()["paused"] is True
    state = client.get("/tickets/governor/state", headers=headers).json()
    assert state["paused"] is True and state["pauseReason"] == "支付网关故障"

    client.post("/tickets/governor/resume", json={"reason": "网关恢复"}, headers=headers)
    assert client.get("/tickets/governor/state", headers=headers).json()["paused"] is False


def test_暂停之后写操作被拦并反映在指标里(client, db_session, monkeypatch):
    from models import CsRefund

    headers = _headers(client)
    _make_admin(client, db_session)
    client.post("/tickets/governor/pause", json={"reason": "错误操作率飙升"}, headers=headers)
    ticket_id = client.post(
        "/tickets", json={"content": "订单 ORD20260115001 要退款 350 元"}, headers=headers
    ).json()["ticketId"]
    _seed_order(db_session, _current_user(client, db_session))
    _script(
        client,
        monkeypatch,
        [
            [
                _understand("refund", amount="350"),
                {"tool_calls": [("create_refund", {"order_no": "ORD20260115001", "amount": "350"})]},
            ],
            [{"text": "这件事需要人工处理。"}],
        ],
    )
    run = client.post(f"/tickets/{ticket_id}/run", headers=headers).json()
    # 暂停时资金类工具仍然要过人的手，但批下去也只会拿到"没有执行"
    assert run["outcome"] == "awaiting_approval"
    decided = client.post(
        f"/tickets/{ticket_id}/decision", json={"approved": True}, headers=headers
    ).json()
    assert db_session.query(CsRefund).count() == 0
    metrics = client.get("/tickets/metrics", headers=headers).json()
    assert metrics["operations"]["blocked"] == 1
    # 被拦下不等于办完：这一步必须交人，而不是给客户一句"已处理"就办结
    assert decided["outcome"] == "escalated"
    assert decided["escalationReason"] == "governor_blocked"


def test_指标给得出那五个核心数(client):
    headers = _headers(client)
    metrics = client.get("/tickets/metrics?days=3", headers=headers).json()
    assert metrics["windowDays"] == 3
    for key in (
        "deflectionRate",
        "humanHandoffRate",
        "avgHandleMinutes",
        "avgFirstResponseMinutes",
        "csatAverage",
        "rejectedAttemptRate",
    ):
        assert key in metrics
    # 一张工单都没有时给 None 而不是 0：0 会被读成"自动解决率是零"，
    # 而真实情况是"还没有数据可算"
    assert metrics["total"] == 0
    assert metrics["deflectionRate"] is None


def test_别人工作区的工单读不到(client, db_session):
    headers = _headers(client)
    ticket_id = client.post("/tickets", json={"content": "我的订单呢"}, headers=headers).json()["ticketId"]
    other = _headers(client, email="bob@example.com", username="bob")
    assert client.get(f"/tickets/{ticket_id}", headers=other).status_code == 404
    assert client.get(f"/tickets/{ticket_id}/events", headers=other).status_code == 404
    assert client.post(f"/tickets/{ticket_id}/run", headers=other).status_code == 404
    # 自己的列表里也不该出现别人那条
    assert client.get("/tickets", headers=other).json()["total"] == 0


# ========== SLA 与指标闭环 ==========


def test_打开队列时超时未办的工单被转走并带上上下文(client, db_session):
    from datetime import timedelta

    from models import Ticket, TicketEvent
    from services.clock import naive_now

    headers = _headers(client)
    ticket_id = client.post(
        "/tickets", json={"content": "ORD-9 到底发没发货"}, headers=headers
    ).json()["ticketId"]
    ticket = db_session.query(Ticket).filter_by(id=ticket_id).one()
    ticket.sla_due_at = naive_now() - timedelta(hours=2)
    db_session.add(
        TicketEvent(
            id="ev-1",
            ticket_id=ticket_id,
            workspace_id=ticket.workspace_id,
            seq=2,
            node="act",
            kind="tool_result",
            status="ok",
            message="查到这单还是 paid",
            created_at=naive_now(),
        )
    )
    db_session.commit()

    body = client.get("/tickets", headers=headers).json()
    assert body["reapedOverdue"] == 1
    assert body["tickets"][0]["status"] == "escalated"
    assert body["tickets"][0]["escalationReason"] == "sla_overdue"
    events = client.get(f"/tickets/{ticket_id}/events", headers=headers).json()["events"]
    handoff = [event for event in events if event["node"] == "escalate"]
    # 文档§4 第 8 步的"带上上下文"：接手人在轨迹里就能看到之前查到过什么
    assert "查到这单还是 paid" in handoff[-1]["message"]


def test_等人批的工单不会被超时抢走(client, db_session):
    """审批收件箱里它有自己的时效呈现，队列这一遍不该把它挪走。"""
    from datetime import timedelta

    from models import Ticket
    from services.clock import naive_now

    headers = _headers(client)
    ticket_id = client.post("/tickets", json={"content": "要退款"}, headers=headers).json()["ticketId"]
    ticket = db_session.query(Ticket).filter_by(id=ticket_id).one()
    ticket.status = "awaiting_approval"
    ticket.sla_due_at = naive_now() - timedelta(hours=5)
    db_session.commit()

    body = client.get("/tickets", headers=headers).json()
    assert body["reapedOverdue"] == 0
    assert body["tickets"][0]["status"] == "awaiting_approval"


def test_评分只收办结过的工单而重复提交取最后一次(client, db_session):
    headers = _headers(client)
    ticket_id = client.post("/tickets", json={"content": "查个物流"}, headers=headers).json()["ticketId"]
    # 还在 new 状态时不收分：给一张没办完的单打分会立刻让指标变噪声
    assert (
        client.post(f"/tickets/{ticket_id}/csat", json={"score": 5}, headers=headers).status_code
        == 409
    )

    closed = client.post(
        f"/tickets/{ticket_id}/close",
        json={"resolution": "answered", "note": "电话说明"},
        headers=headers,
    )
    assert closed.status_code == 200

    assert client.post(f"/tickets/{ticket_id}/csat", json={"score": 4}, headers=headers).status_code == 200
    rescored = client.post(
        f"/tickets/{ticket_id}/csat", json={"score": 2, "comment": "等了三天"}, headers=headers
    ).json()
    assert rescored["csatScore"] == 2

    detail = client.get(f"/tickets/{ticket_id}", headers=headers).json()["ticket"]
    assert detail["csatScore"] == 2 and detail["csatComment"] == "等了三天"
    messages = [
        event["message"]
        for event in client.get(f"/tickets/{ticket_id}/events", headers=headers).json()["events"]
    ]
    # 覆盖评分而不覆盖痕迹：两次都留在轨迹里
    assert sum(1 for message in messages if "客户评分" in message) == 2

    metrics = client.get("/tickets/metrics", headers=headers).json()
    assert metrics["csatResponses"] == 1 and metrics["csatAverage"] == 2


def test_超出范围的评分直接被拒(client):
    headers = _headers(client)
    ticket_id = client.post("/tickets", json={"content": "查物流"}, headers=headers).json()["ticketId"]
    client.post(f"/tickets/{ticket_id}/close", json={"resolution": "answered"}, headers=headers)
    assert client.post(f"/tickets/{ticket_id}/csat", json={"score": 9}, headers=headers).status_code == 422


def test_人工关单把处置累计进客户画像(client, db_session):
    from models import CsCustomer

    headers = _headers(client)
    ticket_id = client.post(
        "/tickets",
        json={"channel": "email", "content": "ORD-9 没发货", "customer_email": "z@corp.com"},
        headers=headers,
    ).json()["ticketId"]
    closed = client.post(
        f"/tickets/{ticket_id}/close",
        json={"resolution": "reassured", "note": "已联系仓库加急"},
        headers=headers,
    ).json()
    assert closed["status"] == "closed"

    detail = client.get(f"/tickets/{ticket_id}", headers=headers).json()["ticket"]
    assert detail["closedAt"] is not None and detail["firstResponseAt"] is not None
    customer = db_session.query(CsCustomer).filter_by(id=detail["customerId"]).one()
    history = json.loads(customer.profile)["history"]
    assert history[0]["ticket_id"] == ticket_id and history[0]["resolution"] == "reassured"


def test_已办结的工单不能再关一次(client):
    headers = _headers(client)
    ticket_id = client.post("/tickets", json={"content": "查物流"}, headers=headers).json()["ticketId"]
    assert client.post(
        f"/tickets/{ticket_id}/close", json={"resolution": "answered"}, headers=headers
    ).status_code == 200
    again = client.post(
        f"/tickets/{ticket_id}/close", json={"resolution": "answered"}, headers=headers
    )
    assert again.status_code == 409


def test_有主的工单只有本人或管理员能关(client, db_session):
    from models import Ticket, User

    headers = _headers(client)
    ticket_id = client.post("/tickets", json={"content": "查物流"}, headers=headers).json()["ticketId"]
    other = _headers(client, email="carol@example.com", username="carol")
    carol = db_session.query(User).filter_by(email="carol@example.com").first()
    alice = db_session.query(User).filter_by(email="alice@example.com").first()
    # 第二个注册的用户默认会拿到自己的工作区，这里并回同一个区才有"同区他人"可言；
    # 角色也要降下来，注册出来的第一个用户总是 admin（见 User.role 的默认值）
    carol.workspace_id = alice.workspace_id
    carol.role = "user"
    db_session.commit()

    refused = client.post(
        f"/tickets/{ticket_id}/close", json={"resolution": "answered"}, headers=other
    )
    assert refused.status_code == 403

    ticket = db_session.query(Ticket).filter_by(id=ticket_id).one()
    ticket.assignee_id = None
    db_session.commit()
    # 没主的单子谁都能接手关掉：这时候不存在"两个人互相覆盖"的问题
    assert client.post(
        f"/tickets/{ticket_id}/close", json={"resolution": "answered"}, headers=other
    ).status_code == 200


# ========== 辅助 ==========


def _current_user(client, db_session):
    from models import User

    return db_session.query(User).filter_by(email="alice@example.com").first()


def _make_admin(client, db_session):
    from models import User

    user = _current_user(client, db_session)
    user.role = "admin"
    db_session.commit()
    return user


# ========== 人工复盘台账 ==========


def _resolved_ticket_with_write(client, db_session, monkeypatch):
    """跑一张会自动执行写操作的工单（改地址是中风险，不需要人批）。"""
    headers = _headers(client)
    ticket_id = client.post(
        "/tickets",
        json={
            "content": "订单 ORD20260115001 的地址写错了，改成上海市浦东新区世纪大道100号",
            "customer_email": "zhang@corp.com",
        },
        headers=headers,
    ).json()["ticketId"]
    _seed_order(db_session, _current_user(client, db_session))
    _script(
        client,
        monkeypatch,
        [
            [
                _understand("change_address"),
                {"tool_calls": [("lookup_order", {"order_no": "ORD20260115001"})]},
                {
                    "tool_calls": [
                        (
                            "update_order_address",
                            {"order_no": "ORD20260115001", "address_text": "上海市浦东新区世纪大道100号"},
                        )
                    ]
                },
                {"text": "地址已改好。"},
            ]
        ],
    )
    assert client.post(f"/tickets/{ticket_id}/run", headers=headers).json()["outcome"] == "resolved"
    return headers, ticket_id


def test_执行过的操作进待复盘队列标注后错误操作率才算得出(client, db_session, monkeypatch):
    from models import CsOperation

    headers, _ticket_id = _resolved_ticket_with_write(client, db_session, monkeypatch)

    queue = client.get("/tickets/operations/unreviewed", headers=headers).json()
    assert queue["count"] == 1
    operation_id = queue["items"][0]["id"]
    assert queue["items"][0]["verdict"] is None
    assert "update_order_address" == queue["items"][0]["tool"]

    metrics = client.get("/tickets/metrics", headers=headers).json()
    # 没人标注的时候不给数：0 会被读成"一次都没错过"，而实际是"还没人看过"
    assert metrics["errorActionRate"] is None
    assert metrics["unreviewedOperations"] == 1

    reviewed = client.post(
        f"/tickets/operations/{operation_id}/review",
        json={"verdict": "wrong", "action": "reverted_address", "note": "客户其实没说要改"},
        headers=headers,
    ).json()
    assert reviewed["verdict"] == "wrong" and reviewed["reviewedBy"]

    metrics = client.get("/tickets/metrics", headers=headers).json()
    assert metrics["errorActionRate"] == 1.0
    assert metrics["reviewedOperations"] == 1
    assert metrics["unreviewedOperations"] == 0
    assert metrics["wrongByCorrectiveAction"] == {"reverted_address": 1}
    assert db_session.query(CsOperation).filter_by(verdict="wrong").count() == 1


def test_判对的操作也要能被标注(client, db_session, monkeypatch):
    headers, _ticket_id = _resolved_ticket_with_write(client, db_session, monkeypatch)
    operation_id = client.get("/tickets/operations/unreviewed", headers=headers).json()["items"][0]["id"]
    client.post(
        f"/tickets/operations/{operation_id}/review", json={"verdict": "ok"}, headers=headers
    )
    metrics = client.get("/tickets/metrics", headers=headers).json()
    assert metrics["errorActionRate"] == 0.0 and metrics["reviewedOperations"] == 1


def test_没执行的操作用不着复盘(client, db_session, monkeypatch):
    headers, _ticket_id = _resolved_ticket_with_write(client, db_session, monkeypatch)
    from models import CsOperation

    # 手工造一条被拦下的账，再拿去标注：应当被拒
    from services.clock import naive_now

    owner = _current_user(client, db_session)
    row = CsOperation(
        id="op-blocked", workspace_id=owner.workspace_id, ticket_id="t-x",
        tool_name="create_refund", operation="refund.create", permission="fund",
        status="blocked", blocked_reason="演练", created_at=naive_now(),
    )
    db_session.add(row)
    db_session.commit()
    refused = client.post(
        "/tickets/operations/op-blocked/review", json={"verdict": "wrong"}, headers=headers
    )
    assert refused.status_code == 400
    assert "不需要复盘" in refused.json()["detail"]


def test_未知的复盘结论被拒(client, db_session, monkeypatch):
    headers, _ticket_id = _resolved_ticket_with_write(client, db_session, monkeypatch)
    operation_id = client.get("/tickets/operations/unreviewed", headers=headers).json()["items"][0]["id"]
    bad = client.post(
        f"/tickets/operations/{operation_id}/review", json={"verdict": "也许吧"}, headers=headers
    )
    assert bad.status_code == 400


def test_别人工作区的账标注不了(client, db_session, monkeypatch):
    headers, _ticket_id = _resolved_ticket_with_write(client, db_session, monkeypatch)
    operation_id = client.get("/tickets/operations/unreviewed", headers=headers).json()["items"][0]["id"]
    other = _headers(client, email="dave@example.com", username="dave")
    refused = client.post(
        f"/tickets/operations/{operation_id}/review", json={"verdict": "ok"}, headers=other
    )
    # 不区分"不存在"与"不是你的"：能用状态码枚举出别人区里有哪些操作 id
    assert refused.status_code == 400
    assert "找不到" in refused.json()["detail"]


# ========== 回复出口 ==========


def test_队列接口说清了有没有接出口通道(client, db_session, monkeypatch):
    headers, ticket_id = _resolved_ticket_with_write(client, db_session, monkeypatch)
    body = client.get("/tickets/outbox", headers=headers).json()
    # 没接通道这件事必须在界面上看得见：否则队列里的 pending 会被读成"马上会发"
    assert body["senderConnected"] is False
    assert body["pending"] >= 1
    kinds = {item["kind"] for item in body["items"]}
    assert {"reply", "csat_invite"} <= kinds
    assert all(item["status"] == "pending" for item in body["items"])


def test_触发发送只有管理员能做而没通道时不假装发掉(client, db_session, monkeypatch):
    headers, _ticket_id = _resolved_ticket_with_write(client, db_session, monkeypatch)
    from models import User

    owner = db_session.query(User).filter_by(email="alice@example.com").first()
    owner.role = "user"
    db_session.commit()
    assert client.post("/tickets/outbox/drain", headers=headers).status_code == 403

    owner.role = "admin"
    db_session.commit()
    summary = client.post("/tickets/outbox/drain", headers=headers).json()
    assert summary["delivered"] is False and summary["sent"] == 0
    assert "没有接入任何发送通道" in summary["reason"]
    assert client.get("/tickets/outbox", headers=headers).json()["pending"] >= 1


def test_抑制一条待发的回复要理由并留下轨迹(client, db_session, monkeypatch):
    headers, ticket_id = _resolved_ticket_with_write(client, db_session, monkeypatch)
    from models import User

    db_session.query(User).filter_by(email="alice@example.com").first().role = "admin"
    db_session.commit()
    row_id = client.get("/tickets/outbox", headers=headers).json()["items"][0]["id"]

    # 空白理由过不了 Pydantic 的长度校验之外的第二道门：写它的地方在 ledger/outbox
    # 那侧（"必须给理由"是业务规则不是字段格式），所以这里是 400 而不是 422
    refused = client.post(
        f"/tickets/outbox/{row_id}/suppress", json={"reason": "  "}, headers=headers
    )
    assert refused.status_code == 400
    assert "理由" in refused.json()["detail"]
    ok = client.post(
        f"/tickets/outbox/{row_id}/suppress",
        json={"reason": "客户已电话解决，别再发邮件"},
        headers=headers,
    )
    assert ok.status_code == 200 and ok.json()["status"] == "suppressed"
    events = client.get(f"/tickets/{ticket_id}/events", headers=headers).json()["events"]
    assert any("被抑制" in (event["message"] or "") for event in events)


# ========== 客户偏好：只有坐席能写 ==========


def _ticket_with_customer(client, headers, email):
    return client.post(
        "/tickets",
        json={"content": "ORD-9 没发货，帮我看看到哪了", "customer_email": email},
        headers=headers,
    ).json()["ticketId"]


def test_坐席记的偏好进画像并去掉首尾空白(client, db_session):
    from models import CsCustomer
    from services.ticket import history as ticket_history

    headers = _headers(client)
    ticket_id = _ticket_with_customer(client, headers, "li@corp.com")

    created = client.post(
        f"/tickets/{ticket_id}/preference",
        json={"preference": "  不要打电话，发邮件  "},
        headers=headers,
    ).json()
    assert created["preferences"] == ["不要打电话，发邮件"]

    detail = client.get(f"/tickets/{ticket_id}", headers=headers).json()["ticket"]
    customer = db_session.query(CsCustomer).filter_by(id=detail["customerId"]).one()
    assert ticket_history.read_profile(customer)["preferences"] == ["不要打电话，发邮件"]


def test_详情接口把画像摆给坐席看(client):
    """看不见就等于不信。坐席要能读到"模型下次会读到什么"，而不是只能选择相信。"""
    headers = _headers(client)
    ticket_id = _ticket_with_customer(client, headers, "kan@corp.com")
    assert client.get(f"/tickets/{ticket_id}", headers=headers).json()[
        "customerPreferences"
    ] == []

    client.post(
        f"/tickets/{ticket_id}/preference",
        json={"preference": "只在工作时间联系"},
        headers=headers,
    )
    body = client.get(f"/tickets/{ticket_id}", headers=headers).json()
    assert body["customerPreferences"] == ["只在工作时间联系"]
    # 画像属于客户不属于工单，所以它摆在 ticket 之外而不是塞进工单对象里
    assert "customerPreferences" not in body["ticket"]


def test_同一条偏好不会被记两次(client):
    """画像会喂给之后每一单的模型，重复三遍"发邮件"就是把三行预算花在废话上。"""
    headers = _headers(client)
    ticket_id = _ticket_with_customer(client, headers, "wu@corp.com")

    for _ in range(3):
        body = client.post(
            f"/tickets/{ticket_id}/preference",
            json={"preference": "工作日晚上别联系"},
            headers=headers,
        ).json()
    assert body["preferences"] == ["工作日晚上别联系"]


def test_记了却读不到等于没记(client, db_session, monkeypatch):
    """钉整条闭环：坐席写 → 画像 → 下一张工单喂给模型的那段摘要。

    写侧和读侧各自都有单测，但它们中间断过一次也没人发现——画像函数改了渲染措辞，
    断言"画像里有这条"照样通过，而模型其实一个字都没多看到。
    """
    from routers import ticket_router
    from conftest import ScriptedAdapter

    headers = _headers(client)
    first = _ticket_with_customer(client, headers, "zhao@corp.com")
    client.post(
        f"/tickets/{first}/preference",
        json={"preference": "不要打电话，发邮件"},
        headers=headers,
    )

    seen = {}

    def _make():
        adapter = ScriptedAdapter(
            [
                {
                    "text": json.dumps(
                        {
                            "intent": "query_logistics",
                            "product": "",
                            "sentiment": "calm",
                            "order_nos": ["ORD-9"],
                            "amount": "",
                            "summary": "查发货",
                        }
                    )
                },
                {"text": "[]"},
                {"text": "查到了吗——先看一眼画像。"},
            ]
        )
        seen["adapter"] = adapter
        return adapter

    monkeypatch.setattr(ticket_router, "_make_adapter", _make)
    second = _ticket_with_customer(client, headers, "zhao@corp.com")
    assert client.post(f"/tickets/{second}/run", headers=headers).status_code == 200

    fed = " ".join(
        str(message.get("content") or "")
        for call in seen["adapter"].calls
        for message in call["messages"]
    )
    assert "不要打电话，发邮件" in fed, "画像没进到模型的上下文里"


def test_没有关联客户的工单记不了偏好(client):
    """宁可报错也不"顺手建一个客户"：把偏好写到错的人身上比不写更难纠正。"""
    headers = _headers(client)
    anonymous = client.post(
        "/tickets", json={"content": "查物流"}, headers=headers
    ).json()["ticketId"]

    refused = client.post(
        f"/tickets/{anonymous}/preference",
        json={"preference": "发邮件别打电话"},
        headers=headers,
    )
    assert refused.status_code == 409


def test_别人的工单不能替他记偏好(client, db_session):
    from models import Ticket, User

    headers = _headers(client)
    ticket_id = _ticket_with_customer(client, headers, "sun@corp.com")
    other = _headers(client, email="bob@example.com", username="bob")
    bob = db_session.query(User).filter_by(email="bob@example.com").first()
    alice = db_session.query(User).filter_by(email="alice@example.com").first()
    bob.workspace_id = alice.workspace_id
    bob.role = "user"
    ticket = db_session.query(Ticket).filter_by(id=ticket_id).one()
    ticket.assignee_id = alice.id
    db_session.commit()

    refused = client.post(
        f"/tickets/{ticket_id}/preference",
        json={"preference": "只接受短信"},
        headers=other,
    )
    assert refused.status_code == 403
    assert client.get(f"/tickets/{ticket_id}", headers=headers).json()["ticket"] is not None


def test_空白偏好不会被当成一条记录(client):
    headers = _headers(client)
    ticket_id = _ticket_with_customer(client, headers, "qian@corp.com")

    too_long = client.post(
        f"/tickets/{ticket_id}/preference", json={"preference": "哈" * 201}, headers=headers
    )
    assert too_long.status_code == 422
    blank = client.post(
        f"/tickets/{ticket_id}/preference", json={"preference": "   "}, headers=headers
    )
    assert blank.status_code == 400
    assert client.get(f"/tickets/{ticket_id}", headers=headers).json()["ticket"] is not None


# ========== 业务数据导入 ==========

_IMPORT_BODY = {
    "customers": [{"email": "buyer@example.com", "phone": "+86 138-0000-0000"}],
    "orders": [
        {
            "order_no": "ORD-7001",
            "status": "paid",
            "total_amount": "350.00",
            "customer": {"email": "buyer@example.com"},
            "items": [{"sku": "SKU-1", "title": "蓝牙耳机", "quantity": 2, "unit_price": "175.00"}],
        }
    ],
}


def test_管理员导入之后工单查得到这张单(client, db_session):
    headers = _headers(client)
    response = client.post("/tickets/cs/import", json=_IMPORT_BODY, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["ordersCreated"] == 1 and body["customersCreated"] == 1

    user = _current_user(client, db_session)
    from services.ticket import business_system

    snapshot = business_system.for_session(db_session, user.workspace_id).get_order("ORD-7001")
    assert snapshot is not None, "接口说写进去了，读侧接缝却查不到"
    assert str(snapshot.total_amount) == "350.00"
    assert [line.sku for line in snapshot.lines] == ["SKU-1"]


def test_普通坐席导不动(client, db_session):
    from models import CsOrder

    headers = _headers(client)
    user = _current_user(client, db_session)
    user.role = "user"  # 注册出来的第一个用户默认 admin
    db_session.commit()

    denied = client.post("/tickets/cs/import", json=_IMPORT_BODY, headers=headers)
    assert denied.status_code == 403
    assert db_session.query(CsOrder).count() == 0, "被拒的那次不该留下半个客户"


def test_域开关关掉时仍然导得进来(client, monkeypatch):
    """这条是**有意的**：那个开关管的是"要不要让模型办工单"，而导数据是打开之前
    就要做的事。一起拦掉的话，第一次部署得"开→导→409→关→再开"，那条顺序没人会走，
    于是这个端点在真实部署里从来没被调过。"""
    monkeypatch.setattr(settings, "TICKET_AGENT_ENABLED", False)
    headers = _headers(client)
    response = client.post("/tickets/cs/import", json=_IMPORT_BODY, headers=headers)
    assert response.status_code == 200, response.text


def test_校验不过把每一行的原因都报回去(client, db_session):
    from models import CsOrder

    headers = _headers(client)
    bad = {
        "orders": [
            {"order_no": "A", "total_amount": "10"},
            {"order_no": "B", "total_amount": "-4"},
            {"order_no": "C", "total_amount": "9", "status": "shiped"},
        ]
    }
    response = client.post("/tickets/cs/import", json=bad, headers=headers)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert {row["ref"] for row in detail} == {"orders[1]", "orders[2]"}
    assert db_session.query(CsOrder).count() == 0, "整批一行都没该落"


def test_导入进的是自己的租户(client, db_session):
    from models import CsOrder

    headers = _headers(client)
    user = _current_user(client, db_session)
    client.post("/tickets/cs/import", json=_IMPORT_BODY, headers=headers)

    row = db_session.query(CsOrder).filter_by(order_no="ORD-7001").one()
    assert row.workspace_id == user.workspace_id


def test_载荷冒充别人的租户被拒(client, db_session):
    headers = _headers(client)
    body = {
        "orders": [
            {"order_no": "H", "total_amount": "1", "workspace_id": "另一个工作区"}
        ]
    }
    response = client.post("/tickets/cs/import", json=body, headers=headers)
    assert response.status_code == 422
    assert "workspace_id" in response.json()["detail"][0]["reason"]


def test_导入留下审计但不带客户隐私(client, db_session):
    from services import audit_log

    headers = _headers(client)
    user = _current_user(client, db_session)
    client.post("/tickets/cs/import", json=_IMPORT_BODY, headers=headers)

    entries = audit_log.history(db_session, user.id)
    mine = [e for e in entries if e["action"] == "cs.import"]
    assert mine, "谁导了这批数据必须能被翻出来"
    dumped = json.dumps(mine[0], ensure_ascii=False)
    assert "buyer@example.com" not in dumped, "审计里存的是计数，不是客户邮箱和地址"
    assert "ORD-7001" not in dumped


