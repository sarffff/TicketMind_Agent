"""业务数据入口：订单与客户真的能进库，且不污染治理账。

这里测的多数不是"功能对不对"，而是**导入不会把别的东西弄坏**——那才是这一层
真正的风险。三条最要紧的：

1. **不写台账。** 当日退款额度聚合的是 ``cs_operations`` 里 ``permission='fund'``
   的行。导入顺手写几笔历史退款进去，症状是"导完数据之后所有退款都被判额度不足
   转人工"——看着像治理在生效，其实是账被污染了。
2. **不覆写画像。** ``profile`` 是系统跨工单学出来的结论，外部文件没资格改它。
3. **全有或全无。** 订单头落了而明细没落，模型读到的是"总额 0 但有商品"，
   它之后的建议建立在自相矛盾的事实上，而这种矛盾在轨迹里看着完全正常。
"""
import uuid
from decimal import Decimal

import pytest

from models import CsCustomer, CsOperation, CsOrder, CsOrderItem
from services.clock import naive_now
from services.ticket import cs_import
from services.ticket.business_system import (
    LOCKED_ORDER_STATUSES,
    ORDER_STATUSES,
    _OPEN_ORDER_STATUSES,
)
from services.ticket.tools import _LOCKED_ORDER_STATUSES

WS = "w1"
OTHER_WS = "w2"
# 校验阶段一行都不碰库，所以纯校验用例可以不给 session
NO_DB = None


def _payload(**overrides) -> dict:
    base = {
        "customers": [
            {"email": "Buyer@Example.com", "phone": "+86 138-0000-0000", "display_name": "李四"},
        ],
        "orders": [
            {
                "order_no": "ORD-9001",
                "status": "paid",
                "total_amount": "128.50",
                "currency": "cny",
                "receiver_name": "李四",
                "address_text": "上海市浦东新区世纪大道 100 号",
                "customer": {"email": "buyer@example.com"},
                "items": [
                    {"sku": "SKU-A", "title": "蓝牙耳机", "quantity": 1, "unit_price": "128.50"}
                ],
                "shipments": [],
            }
        ],
    }
    base.update(overrides)
    return base


def _count(db, model) -> int:
    return db.query(model).count()


# ------------------------------------------------------------------ 端到端


def test_导入之后工具查得到这张单(db_real):
    """这一条是整个问题存在的理由：之前任何生产代码都不创建订单。"""
    from services.ticket import business_system as bs

    result = cs_import.import_cs_data(db_real, WS, _payload())
    db_real.commit()

    assert result.applied, result.rejected
    snapshot = bs.for_session(db_real, WS).get_order("ORD-9001")
    assert snapshot is not None
    assert snapshot.total_amount == Decimal("128.50")
    assert snapshot.refundable == Decimal("128.50")
    assert [line.sku for line in snapshot.lines] == ["SKU-A"]


def test_金额不因浮点而漂(db_real):
    """``Decimal(128.5)`` 是 128.50000000000000565…，而这里算的是"还能退多少"。"""
    cs_import.import_cs_data(db_real, WS, _payload())
    db_real.commit()
    order = db_real.query(CsOrder).filter(CsOrder.order_no == "ORD-9001").one()
    assert str(order.total_amount) == "128.50"
    assert order.currency == "CNY"  # 存前统一大写，否则 cny/CNY 是两个币种


# ------------------------------------------------------------------ 治理边界


def test_导入不写台账(db_real):
    """污染当日退款额度的那条路必须堵死。"""
    cs_import.import_cs_data(
        db_real,
        WS,
        {
            "orders": [
                {
                    "order_no": "ORD-HIST",
                    "total_amount": "500.00",
                    "refunded_amount": "500.00",
                    "status": "refunded",
                }
            ]
        },
    )
    db_real.commit()
    assert _count(db_real, CsOperation) == 0, "导入写了台账，会把历史退款算进今天的额度"


def test_导入模块根本不引用台账或退款表():
    """上一条只挡得住"走这条代码路径"的情况；这条挡"以后有人加进来"。

    断的是**结构**而不是数值：一旦有人觉得"顺手补一笔退款记录更完整"，
    额度账就脏了，而那种改动在 diff 里看着像完善数据。
    """
    import inspect

    source = inspect.getsource(cs_import)
    for forbidden in ("CsOperation", "CsRefund", "ledger"):
        assert forbidden not in source, f"导入模块碰了 {forbidden}"


def test_导入不覆写画像(db_real):
    now = naive_now()
    existing = CsCustomer(
        id=str(uuid.uuid4()),
        workspace_id=WS,
        email="buyer@example.com",
        phone=None,
        display_name="旧名字",
        tier="vip",
        lifetime_amount=Decimal("9999.00"),
        profile='{"preferences": ["偏好顺丰"]}',
        profile_version=3,
        created_at=now,
        updated_at=now,
    )
    db_real.add(existing)
    db_real.commit()

    cs_import.import_cs_data(
        db_real, WS, {"customers": [{"email": "buyer@example.com", "display_name": "新名字"}]}
    )
    db_real.commit()

    assert existing.profile == '{"preferences": ["偏好顺丰"]}'
    assert existing.profile_version == 3
    assert existing.tier == "vip", "文件没提 tier，不该被抹成默认值"
    assert existing.display_name == "新名字"


# ------------------------------------------------------------------ 全有或全无


def test_一行坏则整批不写(db_real):
    good = _payload()["orders"][0]
    bad = {"order_no": "ORD-BAD", "total_amount": "-1.00"}
    result = cs_import.import_cs_data(
        db_real, WS, {"customers": [], "orders": [dict(good), bad]}
    )
    db_real.commit()

    assert not result.applied
    assert _count(db_real, CsOrder) == 0, "那行好的不该落库"


def test_一次报全部错误而不是第一处(db_real):
    result = cs_import.import_cs_data(
        db_real,
        WS,
        {
            "orders": [
                {"order_no": "A", "total_amount": "x"},
                {"order_no": "B", "total_amount": "1", "status": "shippedd"},
                {"order_no": "C", "total_amount": "1", "nonsense": 1},
            ]
        },
    )
    reasons = {r.ref for r in result.rejected}
    assert reasons == {"orders[0]", "orders[1]", "orders[2]"}


def test_载荷里带工作区直接拒(db_real):
    """静默忽略更糟：运营以为导进了 A，实际落在自己的 B 里。"""
    result = cs_import.import_cs_data(
        db_real, WS, {"orders": [{"order_no": "A", "total_amount": "1", "workspace_id": OTHER_WS}]}
    )
    assert not result.applied
    assert _count(db_real, CsOrder) == 0


# ------------------------------------------------------------------ 幂等与合并


def test_同一份文件跑两遍不产生重复(db_real):
    first = cs_import.import_cs_data(db_real, WS, _payload())
    db_real.commit()
    second = cs_import.import_cs_data(db_real, WS, _payload())
    db_real.commit()

    assert first.orders_created == 1 and second.orders_updated == 1
    assert second.orders_created == 0
    assert _count(db_real, CsOrder) == 1
    assert _count(db_real, CsOrderItem) == 1
    assert _count(db_real, CsCustomer) == 1


def test_重跑不会把历史订单挪到今天(db_real):
    past = "2025-03-01T10:00:00"
    cs_import.import_cs_data(
        db_real, WS, {"orders": [{"order_no": "OLD", "total_amount": "1", "created_at": past}]}
    )
    db_real.commit()
    created = db_real.query(CsOrder).filter(CsOrder.order_no == "OLD").one().created_at

    # 第二遍不带 created_at：那说的是"文件没提"，不是"改成现在"
    cs_import.import_cs_data(db_real, WS, {"orders": [{"order_no": "OLD", "total_amount": "2"}]})
    db_real.commit()
    assert db_real.query(CsOrder).filter(CsOrder.order_no == "OLD").one().created_at == created


def test_文件少一行商品不等于删除(db_real):
    cs_import.import_cs_data(
        db_real,
        WS,
        {
            "orders": [
                {
                    "order_no": "M",
                    "total_amount": "300",
                    "items": [
                        {"sku": "S1", "title": "一", "quantity": 1, "unit_price": "100"},
                        {"sku": "S2", "title": "二", "quantity": 1, "unit_price": "200"},
                    ],
                }
            ]
        },
    )
    db_real.commit()
    cs_import.import_cs_data(
        db_real,
        WS,
        {
            "orders": [
                {
                    "order_no": "M",
                    "total_amount": "100",
                    "items": [{"sku": "S1", "title": "一", "quantity": 1, "unit_price": "100"}],
                }
            ]
        },
    )
    db_real.commit()
    assert _count(db_real, CsOrderItem) == 2, "导出漏一行不该被推断成删除"


def test_同号订单在不同工作区各建各的(db_real):
    """租户边界：不能因为别家已经有 ORD-1 就认为这张单属于这里。"""
    cs_import.import_cs_data(db_real, WS, {"orders": [{"order_no": "SHARED", "total_amount": "1"}]})
    cs_import.import_cs_data(
        db_real, OTHER_WS, {"orders": [{"order_no": "SHARED", "total_amount": "7"}]}
    )
    db_real.commit()
    assert _count(db_real, CsOrder) == 2
    other = db_real.query(CsOrder).filter(CsOrder.workspace_id == OTHER_WS).one()
    assert other.total_amount == Decimal("7.00")


# ------------------------------------------------------------------ 身份归一


def test_带国际区号的电话认成同一个人(db_real):
    """归一化只在 intake 做一次。各削一遍的话同一个人会分裂成两行，画像与限额都分裂。"""
    cs_import.import_cs_data(db_real, WS, {"customers": [{"phone": "+86 138-0000-0000"}]})
    db_real.commit()
    result = cs_import.import_cs_data(db_real, WS, {"customers": [{"phone": "13800000000"}]})
    db_real.commit()

    assert result.customers_updated == 1, "同一个人被当成了两个人"
    assert _count(db_real, CsCustomer) == 1


def test_邮箱大小写认成同一个人(db_real):
    cs_import.import_cs_data(db_real, WS, {"customers": [{"email": "Buyer@Example.com"}]})
    db_real.commit()
    result = cs_import.import_cs_data(db_real, WS, {"customers": [{"email": "buyer@example.com"}]})
    db_real.commit()
    assert _count(db_real, CsCustomer) == 1
    assert result.customers_updated == 1


# ------------------------------------------------------------------ 校验规则


@pytest.mark.parametrize(
    "order,expect",
    [
        ({"total_amount": "10", "refunded_amount": "20"}, "大于"),
        ({"total_amount": "10", "status": "not_a_status"}, "status"),
        ({"total_amount": "10", "created_at": "2099-01-01T00:00:00"}, "未来"),
        ({"total_amount": True}, "不是金额"),
        ({"total_amount": "NaN"}, "金额"),
        ({"total_amount": "10", "order_no": ""}, "order_no"),
        ({"total_amount": "10" * 30}, "容量"),
    ],
)
def test_坏数据被挡在门外(order, expect):
    row = {"order_no": "X", **order} if "order_no" not in order else order
    result = cs_import.import_cs_data(NO_DB, WS, {"orders": [row]})
    assert not result.applied
    assert expect in " ".join(r.reason for r in result.rejected), result.rejected


def test_超额退款的历史值仍然守得住余额(db_real):
    """refunded_amount 是"还能退多少"的第二道保险，导入不能把它冲掉。"""
    cs_import.import_cs_data(
        db_real,
        WS,
        {"orders": [{"order_no": "P", "total_amount": "100.00", "refunded_amount": "60.00"}]},
    )
    db_real.commit()
    from services.ticket import business_system as bs

    assert bs.for_session(db_real, WS).get_order("P").refundable == Decimal("40.00")


def test_换主人被拒(db_real):
    cs_import.import_cs_data(db_real, WS, _two_people())
    db_real.commit()
    result = cs_import.import_cs_data(
        db_real,
        WS,
        {"orders": [{"order_no": "O", "total_amount": "1", "customer": {"email": "b@b.com"}}]},
    )
    assert not result.applied
    assert "客户" in result.rejected[0].reason


def test_订单引用陌生客户时是拒绝而不是自动建档(db_real):
    """自动建一个身份壳在这里是有害的：邮箱打错一个字就多出一个幽灵客户，
    而画像与限额从此分裂在两行上——``intake`` 敢自动建是因为工单必须能进来，
    导入没有这个约束，说清楚要比猜得好。"""
    result = cs_import.import_cs_data(
        db_real, WS, {"orders": [{"order_no": "O", "total_amount": "1", "customer": {"email": "x@x.com"}}]}
    )
    assert not result.applied
    assert _count(db_real, CsCustomer) == 0
    assert "customers" in result.rejected[0].reason, "报错要指出该拿什么补"


def _two_people() -> dict:
    return {
        "customers": [{"email": "a@a.com"}, {"email": "b@b.com"}],
        "orders": [{"order_no": "O", "total_amount": "1", "customer": {"email": "a@a.com"}}],
    }



def test_一批里两个客户抢同一身份被拒(db_real):
    result = cs_import.import_cs_data(
        db_real,
        WS,
        {"customers": [{"email": "d@d.com"}, {"email": "d@d.com", "display_name": "另一个"}]},
    )
    assert not result.applied
    assert result.rejected[0].ref == "customers[1]"


def test_一批里订单号重复被拒(db_real):
    result = cs_import.import_cs_data(
        db_real, WS, {"orders": [{"order_no": "D", "total_amount": "1"}, {"order_no": "D", "total_amount": "2"}]}
    )
    assert not result.applied


def test_超出批量上限被拒(db_real):
    rows = [{"order_no": f"N{i}", "total_amount": "1"} for i in range(cs_import.MAX_ORDERS + 1)]
    result = cs_import.import_cs_data(NO_DB, WS, {"orders": rows})
    assert not result.applied
    assert result.rejected[0].kind == "payload"


# ------------------------------------------------------------------ 口径唯一


def test_状态宇宙只有一份定义():
    """``LOCKED_ORDER_STATUSES`` 现在是从全集推出来的。这条钉住推导本身：
    新增一个状态时如果忘了分类，它必须仍然落在"已锁"那一侧——对退款而言
    判成"不许动"是安全的，判成"还能动"是要出钱的。"""
    assert ORDER_STATUSES == _OPEN_ORDER_STATUSES | LOCKED_ORDER_STATUSES
    assert not (_OPEN_ORDER_STATUSES & LOCKED_ORDER_STATUSES)
    assert _LOCKED_ORDER_STATUSES is LOCKED_ORDER_STATUSES, "tools.py 又抄了一份"


def test_示例数据过得了自己的校验(db_real):
    """``scripts/seed_cs_demo.py`` 里那份是给人看的样例。样例一旦躲过校验规则，
    它就会教出一套错误的写法——所以这里把它当真数据跑一遍。
    """
    import sys

    sys.path.insert(0, "scripts")
    import importlib

    seed = importlib.import_module("seed_cs_demo")

    result = cs_import.import_cs_data(db_real, WS, seed.DEMO)
    db_real.commit()
    assert result.applied, [(r.ref, r.reason) for r in result.rejected]

    again = cs_import.import_cs_data(db_real, WS, seed.DEMO)
    db_real.commit()
    assert again.orders_created == 0 and again.orders_updated == len(seed.DEMO["orders"])
    assert _count(db_real, CsOperation) == 0, "示例数据不该动治理账"


def test_示例数据覆盖到该覆盖的状态(db_real):
    """这份样例的价值在于每个状态都踩一条治理规则。少一个的话，评估之外的
    那条路径就又没人走过了。"""
    import importlib
    import sys

    sys.path.insert(0, "scripts")
    seed = importlib.import_module("seed_cs_demo")
    cs_import.import_cs_data(db_real, WS, seed.DEMO)
    db_real.commit()

    statuses = {row.status for row in db_real.query(CsOrder).all()}
    assert {"paid", "packed", "shipped", "delivered", "refunding"} <= statuses
    # 部分退过的那张才是"超额"用例的唯一输入
    assert any(
        row.refunded_amount > 0 for row in db_real.query(CsOrder).all()
    ), "没有部分退款的样例，可退余额那条规则测不到"

