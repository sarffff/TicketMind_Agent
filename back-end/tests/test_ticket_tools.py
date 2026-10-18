"""能力层：工具面装配、幂等、权限档位、业务状态校验与治理拦截。

用 ``db_real``：这里要测的正是一次写操作在库里留下的痕迹——账本行、业务行、
以及重复调用时那一行有没有被改两次。
"""
import json
import uuid
from decimal import Decimal

import pytest

from config import settings
from conftest import run
from models import CsCustomer, CsInvoice, CsOperation, CsOrder, CsOrderItem, CsRefund, CsShipment
from services.clock import naive_now
from services.model_adapter import ToolCall
from services.ticket import governor, tools
from services.tool_runtime import ToolRuntime, ToolStatus

WS = "w1"


@pytest.fixture(autouse=True)
def _ticket_settings(monkeypatch):
    monkeypatch.setattr(settings, "TICKET_DAILY_REFUND_LIMIT", 1000.0)
    monkeypatch.setattr(settings, "TICKET_REFUND_REVIEW_THRESHOLD", 200.0)


def _order(
    db,
    *,
    order_no="ORD20260115001",
    status="paid",
    total="350.00",
    refunded="0.00",
    customer_id="cust1",
):
    now = naive_now()
    order = CsOrder(
        id=str(uuid.uuid4()),
        workspace_id=WS,
        customer_id=customer_id,
        order_no=order_no,
        status=status,
        total_amount=Decimal(total),
        refunded_amount=Decimal(refunded),
        receiver_name="张三",
        receiver_phone="13800138000",
        address_text="北京市海淀区中关村大街1号",
        created_at=now,
        updated_at=now,
    )
    db.add(order)
    db.add(
        CsOrderItem(
            id=str(uuid.uuid4()),
            order_id=order.id,
            sku="EAR-001",
            title="蓝牙耳机",
            quantity=1,
            unit_price=Decimal(total),
            created_at=now,
        )
    )
    db.commit()
    return order


# ========== 查询 ==========


def test_查订单给出可退余额与商品行(db_real):
    _order(db_real)
    text = tools.lookup_order(db_real, WS, order_no="ORD20260115001")
    assert "状态 paid" in text
    assert "可退 350.00 元" in text
    assert "蓝牙耳机（EAR-001）×1" in text
    assert "海淀区" in text


def test_查无此单时让客户回头确认而不是猜一个相近的(db_real):
    with pytest.raises(tools.BusinessReject) as exc:
        tools.lookup_order(db_real, WS, order_no="ORD000000000000")
    assert "不要换一个看起来相近的号再试" in exc.value.message


def test_没发货的订单不鼓励模型承诺时效(db_real):
    _order(db_real, order_no="ORDNONE", status="paid")
    with pytest.raises(tools.BusinessReject) as exc:
        tools.lookup_logistics(db_real, WS, order_no="ORDNONE")
    assert "没有发货不等于丢件" in exc.value.message


def test_查物流可以按运单号(db_real):
    order = _order(db_real, status="shipped")
    now = naive_now()
    db_real.add(
        CsShipment(
            id=str(uuid.uuid4()),
            order_id=order.id,
            carrier="顺丰",
            tracking_no="SF1234567890",
            status="in_transit",
            last_event="已到北京中转场",
            created_at=now,
            updated_at=now,
        )
    )
    db_real.commit()
    by_order = tools.lookup_logistics(db_real, WS, order_no=order.order_no)
    by_tracking = tools.lookup_logistics(db_real, WS, tracking_no="SF1234567890")
    assert "顺丰 SF1234567890：in_transit" in by_order
    assert "已到北京中转场" in by_tracking


def test_别的工作区的运单号查不到(db_real):
    """``cs_shipments`` 没有 workspace_id 列，归属跟着订单走。

    这条查询原来是 ``filter(tracking_no == ...)`` 一把梭——按"运单号全局唯一"的直觉
    查，于是任何工作区的 Agent 拿一个别家的运单号就能看到承运商、状态和最新事件。
    这张表上每一条查询都做了租户隔离，只有这一条漏了，而漏的原因恰好是"运单号本来就
    是唯一的"这个直觉是对的、语义却不对：唯一的是号码，可读的人不是所有人。
    """
    now = naive_now()
    foreign = CsOrder(
        id=str(uuid.uuid4()),
        workspace_id="w-other",
        customer_id=None,
        order_no="ORD-Foreign",
        status="shipped",
        currency="CNY",
        total_amount=Decimal("99.00"),
        refunded_amount=Decimal("0.00"),
        created_at=now,
        updated_at=now,
    )
    db_real.add(foreign)
    db_real.flush()
    db_real.add(
        CsShipment(
            id=str(uuid.uuid4()),
            order_id=foreign.id,
            carrier="顺丰",
            tracking_no="SF-FOREIGN-1",
            status="in_transit",
            last_event="已到达上海中转场",
            created_at=now,
            updated_at=now,
        )
    )
    db_real.commit()

    with pytest.raises(tools.BusinessReject) as exc:
        tools.lookup_logistics(db_real, WS, tracking_no="SF-FOREIGN-1")
    # 报错不能泄露"它存在"，否则就是一条跨租户的探测信道
    assert "查无运单" in exc.value.message
    assert "上海" not in exc.value.message


def test_查客户带上风险标记与在途订单(db_real):
    now = naive_now()
    db_real.add(
        CsCustomer(
            id="cust1",
            workspace_id=WS,
            display_name="李四",
            tier="vip",
            email="li@corp.com",
            lifetime_amount=Decimal("9800"),
            risk_flags=json.dumps(["投诉史"], ensure_ascii=False),
            created_at=now,
            updated_at=now,
        )
    )
    _order(db_real, customer_id="cust1", status="paid")
    _order(db_real, order_no="ORDOLD", customer_id="cust1", status="delivered")
    db_real.commit()

    text = tools.lookup_customer(db_real, WS, email="li@corp.com")
    assert "档位 vip" in text and "投诉史" in text
    # delivered 那笔不在途：客户问"我还有什么没到"时不要把已签收的也算进去
    assert "在途订单 1 笔" in text and "ORD20260115001" in text and "ORDOLD" not in text


def test_查询条件一个都不给时是中文说明而不是堆栈(db_real):
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        tools.LookupCustomerArgs.model_validate({})
    assert "至少要给一个" in tools._humanize(exc.value)


# ========== 写操作与幂等 ==========


def test_改地址落到订单并记一笔账(db_real):
    order = _order(db_real)
    text = tools.update_order_address(
        db_real, WS, order_no=order.order_no, address_text="上海市浦东新区世纪大道100号",
        ticket_id="t1", actor="u1",
    )
    db_real.commit()
    assert "已更新" in text and "世纪大道" in text
    assert order.address_text == "上海市浦东新区世纪大道100号"
    row = db_real.query(CsOperation).one()
    assert (row.status, row.permission, row.tool_name) == ("executed", "mutate", "update_order_address")
    assert row.actor == "u1"


def test_已发货的单改不了地址而这次尝试仍然留账(db_real):
    order = _order(db_real, status="shipped")
    with pytest.raises(tools.BusinessReject) as exc:
        tools.update_order_address(
            db_real, WS, order_no=order.order_no, address_text="换到上海", ticket_id="t1"
        )
    db_real.commit()
    assert "改不了了" in exc.value.message
    assert order.address_text.endswith("1号")  # 原样未动
    row = db_real.query(CsOperation).one()
    assert row.status == "failed" and row.permission == "mutate"


def test_同一笔退款第二次不会退两次(db_real):
    order = _order(db_real)
    first = tools.create_refund(db_real, WS, order_no=order.order_no, amount="350", ticket_id="t1")
    db_real.commit()
    assert "退款已发起" in first

    second = tools.create_refund(db_real, WS, order_no=order.order_no, amount="350", ticket_id="t1")
    db_real.commit()
    assert second.startswith("重复调用")
    assert "首次的结果是" in second
    assert db_real.query(CsRefund).count() == 1
    assert db_real.query(CsOperation).count() == 1
    assert order.refunded_amount == Decimal("350.00")
    assert order.status == "refunded"


def test_显式幂等键优先于推导键(db_real):
    order = _order(db_real)
    tools.create_refund(
        db_real, WS, order_no=order.order_no, amount="100", ticket_id="t1", idempotency_key="human-edit-1"
    )
    db_real.commit()
    # 人改了金额、换了键：那是新的一笔，不是重复
    tools.create_refund(
        db_real, WS, order_no=order.order_no, amount="120", ticket_id="t1", idempotency_key="human-edit-2"
    )
    db_real.commit()
    assert db_real.query(CsRefund).count() == 2
    assert order.refunded_amount == Decimal("220")


def test_退款超出可退余额时不建退款单但记下这次尝试(db_real):
    order = _order(db_real, total="350.00", refunded="100.00")
    with pytest.raises(tools.BusinessReject) as exc:
        tools.create_refund(db_real, WS, order_no=order.order_no, amount="400", ticket_id="t1")
    db_real.commit()
    assert "超出可退余额" in exc.value.message and "最多可退 250.00 元" in exc.value.message
    assert db_real.query(CsRefund).count() == 0
    assert order.refunded_amount == Decimal("100.00")
    assert db_real.query(CsOperation).one().status == "failed"


def test_金额写法不成立时不猜一个数也不记账(db_real):
    # 参数不成立不是一次业务动作，账本记的是业务动作
    order = _order(db_real)
    with pytest.raises(tools.BusinessReject) as exc:
        tools.create_refund(db_real, WS, order_no=order.order_no, amount="三百", ticket_id="t1")
    db_real.commit()
    assert "退款金额不成立" in exc.value.message
    assert db_real.query(CsOperation).count() == 0


def test_全额退款把订单状态推到已退款(db_real):
    order = _order(db_real)
    tools.create_refund(db_real, WS, order_no=order.order_no, amount="150", ticket_id="t1")
    db_real.commit()
    assert order.status == "paid"
    tools.create_refund(db_real, WS, order_no=order.order_no, amount="200", ticket_id="t1")
    db_real.commit()
    assert order.status == "refunded"


def test_取消订单不等于退款(db_real):
    order = _order(db_real)
    text = tools.cancel_order(db_real, WS, order_no=order.order_no, reason="不想要了", ticket_id="t1")
    db_real.commit()
    assert order.status == "cancelled" and order.cancelled_at is not None
    assert order.refunded_amount == Decimal("0.00")
    assert db_real.query(CsRefund).count() == 0
    assert "款项还没有退" in text


def test_已发货的订单取消不掉(db_real):
    order = _order(db_real, status="shipped")
    with pytest.raises(tools.BusinessReject) as exc:
        tools.cancel_order(db_real, WS, order_no=order.order_no, ticket_id="t1")
    db_real.commit()
    assert "取消不掉" in exc.value.message
    assert order.status == "shipped"


def test_同一张工单重复申请同一张发票返回首次结果(db_real):
    order = _order(db_real)
    first = tools.request_invoice(
        db_real, WS, order_no=order.order_no, title="某某科技有限公司", ticket_id="t1"
    )
    db_real.commit()
    assert "已提交" in first

    second = tools.request_invoice(
        db_real, WS, order_no=order.order_no, title="某某科技有限公司", ticket_id="t1"
    )
    db_real.commit()
    # 幂等先于业务校验：重试一笔办好的事要拿回"这笔做过"，
    # 而不是"已经申请过"——后者会让模型以为这是新问题，转去试一件更糟的
    assert second.startswith("重复调用")
    assert db_real.query(CsInvoice).count() == 1


def test_换个工单再申请同一抬头仍然被拒(db_real):
    order = _order(db_real)
    tools.request_invoice(
        db_real, WS, order_no=order.order_no, title="某某科技有限公司", ticket_id="t1"
    )
    db_real.commit()
    with pytest.raises(tools.BusinessReject) as exc:
        tools.request_invoice(
            db_real, WS, order_no=order.order_no, title="某某科技有限公司", ticket_id="t2"
        )
    db_real.commit()
    assert "已经申请过" in exc.value.message
    assert db_real.query(CsInvoice).count() == 1


# ========== 治理拦截 ==========


def test_全局暂停时写操作被拦而业务数据没动(db_real):
    order = _order(db_real)
    governor.pause(db_real, WS, actor_id="u9", reason="支付网关故障")
    db_real.commit()

    text = tools.create_refund(db_real, WS, order_no=order.order_no, amount="350", ticket_id="t1")
    db_real.commit()
    assert "这次操作没有执行" in text and "支付网关故障" in text
    assert db_real.query(CsRefund).count() == 0
    assert order.refunded_amount == Decimal("0.00")
    row = db_real.query(CsOperation).one()
    assert row.status == "blocked" and row.permission == "fund"


def test_当日额度用尽后第二笔被拦(db_real):
    row = governor.get_or_create(db_real, WS)
    row.daily_refund_limit = Decimal("500")
    db_real.commit()

    _order(db_real, order_no="ORDA", total="600.00")
    _order(db_real, order_no="ORDB", total="600.00")
    first = tools.create_refund(db_real, WS, order_no="ORDA", amount="400", ticket_id="ta")
    db_real.commit()
    assert "退款已发起" in first

    second = tools.create_refund(db_real, WS, order_no="ORDB", amount="200", ticket_id="tb")
    db_real.commit()
    assert "当日退款额度不足" in second
    assert db_real.query(CsRefund).count() == 1


def test_改变过状态的那次写进了防篡改审计链而拦下和失败的没有(db_real):
    from services import audit_log

    order = _order(db_real)
    tools.update_order_address(
        db_real, WS, order_no=order.order_no,
        address_text="上海市浦东新区世纪大道100号", ticket_id="t1", actor="u1",
    )
    db_real.commit()
    # 一次改动过状态，一次只是试过了（已发货，取消不掉）
    shipped = _order(db_real, order_no="ORDB", status="shipped")
    with pytest.raises(tools.BusinessReject):
        tools.cancel_order(db_real, WS, order_no=shipped.order_no, ticket_id="t1", actor="u1")
    db_real.commit()
    # 再一笔被治理拦下的
    governor.pause(db_real, WS, actor_id="u1", reason="演练")
    db_real.commit()
    tools.create_refund(db_real, WS, order_no=order.order_no, amount="10", ticket_id="t1", actor="u1")
    db_real.commit()

    entries = audit_log.history(db_real, "u1")
    # 链上只有一条：审计回答"这串动作有没有被事后改过"，把没发生的动作也钉进去
    # 就退化成流水日志了；那两次的证据在 cs_operations 里（failed 与 blocked）
    assert [entry["action"] for entry in entries] == ["ticket.write"]
    assert entries[0]["target"].startswith("order.update_address:")
    assert entries[0]["ticketId"] == "t1"
    assert audit_log.verify(db_real, "u1")["ok"] is True
    assert db_real.query(CsOperation).filter_by(status="executed").count() == 1
    assert db_real.query(CsOperation).filter_by(status="failed").count() == 1
    assert db_real.query(CsOperation).filter_by(status="blocked").count() == 1


# ========== 工具面与运行时契约 ==========


def test_工具面按风险档位装配(db_real):
    _order(db_real)
    low = [d.name for d in tools.build(db_real, workspace_id=WS, user_id="u1", max_risk="low")]
    mid = [d.name for d in tools.build(db_real, workspace_id=WS, user_id="u1", max_risk="mid")]
    high = [d.name for d in tools.build(db_real, workspace_id=WS, user_id="u1", max_risk="high")]
    assert set(low) == {"lookup_order", "lookup_logistics", "lookup_customer"}
    assert "update_order_address" in mid and "create_refund" not in mid
    assert {"create_refund", "cancel_order"} <= set(high)
    # 忘了传风险等级 = 只给查询，而不是拿着退款工具去办查物流的工单
    assert [d.name for d in tools.build(db_real, workspace_id=WS, user_id="u1")] == low


def test_资金类工具就是要人批(db_real):
    assert tools.requires_approval("create_refund")
    assert tools.requires_approval("cancel_order")
    for name in ("lookup_order", "update_order_address", "request_invoice"):
        assert not tools.requires_approval(name)


def test_生成的schema能过运行时的校验(db_real):
    _order(db_real)
    runtime = ToolRuntime(
        tools.build(db_real, workspace_id=WS, user_id="u1", max_risk="high")
    )
    ok = run(runtime.execute(ToolCall(id="c1", name="lookup_order", arguments='{"order_no": "ORD20260115001"}')))
    assert ok.status is ToolStatus.OK and "可退 350.00 元" in ok.content

    # 缺字段
    missing = run(runtime.execute(ToolCall(id="c2", name="lookup_order", arguments="{}")))
    assert missing.status is ToolStatus.INVALID_ARGUMENTS

    # 多写的键由 forbid 生成的 schema 拦下
    extra = run(
        runtime.execute(
            ToolCall(id="c3", name="lookup_order", arguments='{"order_no": "X", "customer_id": "y"}')
        )
    )
    assert extra.status is ToolStatus.INVALID_ARGUMENTS

    # 未注册的工具不落到 handler
    unknown = run(runtime.execute(ToolCall(id="c4", name="delete_everything", arguments="{}")))
    assert unknown.status is ToolStatus.INVALID_ARGUMENTS


def test_高风险工具在低档工具面上根本不存在(db_real):
    runtime = ToolRuntime(tools.build(db_real, workspace_id=WS, user_id="u1", max_risk="low"))
    refused = run(
        runtime.execute(
            ToolCall(id="c5", name="create_refund", arguments='{"order_no": "ORD1", "amount": "10"}')
        )
    )
    assert refused.status is ToolStatus.INVALID_ARGUMENTS
    assert "未注册的工具" in refused.content
