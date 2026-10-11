"""业务系统边界：值对象不越过接口，租户隔离住在实现里。

这一层是**接缝**，不是功能。所以这里测的全是"形状"——那些将来接真实 ERP 时
一旦破掉就会静默退化的性质：

1. **ORM 实体不许越过接口。** 只要 ``OrderSnapshot`` 里还挂着一个 ``CsOrder``，
   换实现就等于要求对方也造得出 SQLAlchemy 实体，接缝就白立了。这条用
   "返回的对象类型是 dataclass 而不是 ORM 映射类"来钉。
2. **租户隔离住在实现里，不是调用方的自觉。** ``cs_shipments`` 没有
   ``workspace_id`` 列，归属跟着订单走。原来按运单号查物流是一句裸 ``filter``
   一把梭，任何工作区都能凭一个运单号看到别家的物流状态。
3. **口径只有一份。** "在途订单"那四个状态值原来在 tools.py 里，现在在实现里，
   两处都有一份的话它们**后来**会漂移——而漂移的表现是"客户名下没有待办订单"。
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.orm import DeclarativeBase

from models import CsCustomer, CsOrder, CsOrderItem, CsShipment
from services.clock import naive_now
from services.ticket import business_system as bs
from services.ticket.business_system import LocalCsBusinessSystem

WS = "w1"


def _system(db) -> LocalCsBusinessSystem:
    return bs.for_session(db, WS)


def _order(db, *, order_no="ORD-1", status="paid", total="350.00", refunded="0.00", workspace=WS):
    now = naive_now()
    order = CsOrder(
        id=str(uuid.uuid4()), workspace_id=workspace, customer_id=None, order_no=order_no,
        status=status, currency="CNY", total_amount=Decimal(total),
        refunded_amount=Decimal(refunded), receiver_name="张三",
        receiver_phone="13800000000", address_text="北京市海淀区中关村大街1号",
        created_at=now, updated_at=now,
    )
    db.add(order)
    db.flush()
    db.add(
        CsOrderItem(
            id=str(uuid.uuid4()), order_id=order.id, title="蓝牙耳机", sku="SKU-1",
            quantity=2, unit_price=Decimal("175.00"), line_status="normal",
            created_at=now,
        )
    )
    db.commit()
    return order


def _shipment(db, order, *, tracking="SF-1"):
    now = naive_now()
    row = CsShipment(
        id=str(uuid.uuid4()), order_id=order.id, carrier="顺丰",
        tracking_no=tracking, status="in_transit", last_event="已到达上海中转场",
        created_at=now, updated_at=now,
    )
    db.add(row)
    db.commit()
    return row


# ========== 接口形状 ==========


def test_实现满足协议():
    assert isinstance(LocalCsBusinessSystem.__new__(LocalCsBusinessSystem), bs.BusinessSystem)


def test_返回的是值对象而不是ORM实体(db_real):
    """这条是整个接缝的意义所在。

    只要快照里还挂着 ``CsOrder``，"换个后端"就等于要求对方也造得出 SQLAlchemy
    实体——那层抽象会在第一次真实集成时当场失效，而那时已经改不动了。
    """
    order = _order(db_real)
    snapshot = _system(db_real).get_order(order.order_no)

    assert isinstance(snapshot, bs.OrderSnapshot)
    assert not isinstance(snapshot, DeclarativeBase)
    assert not any(isinstance(value, DeclarativeBase) for value in snapshot.shipments)
    assert not any(isinstance(value, DeclarativeBase) for value in snapshot.lines)
    # 值对象是 frozen 的：改它不该成为写路径的一部分
    with pytest.raises(Exception):
        snapshot.status = "refunded"


def test_可退余额由快照自己算(db_real):
    """写路径的余额校验以后也要读这个数，所以它必须只有一个算法。"""
    order = _order(db_real, total="350.00", refunded="100.00")
    snapshot = _system(db_real).get_order(order.order_no)

    assert snapshot.refundable == snapshot.total_amount - snapshot.refunded_amount


# ========== 读的行为与直接 ORM 一致 ==========


def test_查不到订单返回None而不是抛(db_real):
    assert _system(db_real).get_order("ORD-不存在") is None


def test_运单号查询带上了租户隔离(db_real):
    """隔离规则住在实现里，调用方拿不到"绕过 join 的裸查询"这个选项。"""
    foreign = _order(db_real, order_no="ORD-F", workspace="w-other")
    _shipment(db_real, foreign, tracking="SF-FOREIGN")

    assert _system(db_real).find_shipments_by_tracking("SF-FOREIGN") == ()

    mine = _order(db_real, order_no="ORD-M", workspace=WS)
    _shipment(db_real, mine, tracking="SF-MINE")
    found = _system(db_real).find_shipments_by_tracking("SF-MINE")

    assert [item.tracking_no for item in found] == ["SF-MINE"]
    assert found[0].last_event == "已到达上海中转场"


def test_空运单号不查全表(db_real):
    """``filter(tracking_no == "")`` 会匹配到所有空号的行——那是一次意外的全表读。"""
    order = _order(db_real)
    _shipment(db_real, order, tracking="SF-1")

    assert _system(db_real).find_shipments_by_tracking("   ") == ()


def test_客户按邮箱查时大小写归一(db_real):
    now = naive_now()
    db_real.add(
        CsCustomer(
            id="c1", workspace_id=WS, display_name="李四", email="buyer@example.com",
            phone="13800000000", tier="gold", lifetime_amount=Decimal("1200.00"),
            profile=None, profile_version=0, created_at=now, updated_at=now,
        )
    )
    db_real.commit()
    system = _system(db_real)

    assert system.find_customer(email="buyer@example.com").id == "c1"
    assert system.find_customer(email="BUYER@EXAMPLE.COM").id == "c1"
    assert system.find_customer(phone="138-0000-0000").id == "c1"
    assert system.find_customer(email="nobody@example.com") is None


def test_风险标记坏掉时不影响整次查询(db_real):
    """那一列存的是 JSON 文本。读不懂就当没有，不该让一次客户查询失败。"""
    now = naive_now()
    db_real.add(
        CsCustomer(
            id="c2", workspace_id=WS, display_name="王五", email="w@corp.com",
            phone=None, tier="normal", lifetime_amount=Decimal("0.00"),
            risk_flags="{不是 JSON", profile=None, profile_version=0,
            created_at=now, updated_at=now,
        )
    )
    db_real.commit()

    customer = _system(db_real).find_customer(email="w@corp.com")
    assert customer.risk_flags == ()


def test_在途订单的口径只有一份():
    """两份相等的清单是漂移的起点，所以这里查的是"只有一处定义"。"""
    import pathlib
    import re

    root = pathlib.Path(bs.__file__).parent
    definers = [
        path.name
        for path in root.glob("*.py")
        if re.search(r"^_OPEN_ORDER_STATUSES\s*=", path.read_text(encoding="utf-8"), re.M)
    ]
    assert definers == ["business_system.py"], f"在途口径被抄成了多份：{definers}"
    assert bs._OPEN_ORDER_STATUSES == frozenset(
        {"pending_payment", "paid", "packed", "refunding"}
    ), "这四个值决定界面上「在途订单 N 笔」是多少，改它要有意识"
