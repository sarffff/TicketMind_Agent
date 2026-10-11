"""业务系统边界：订单 / 物流 / 客户这三样**不是我们的数据**。

``cs_orders`` / ``cs_shipments`` / ``cs_customers`` 今天住在同一个 MySQL 里，用
SQLAlchemy 直接查。那是替身，不是集成：轨迹里写"已执行退款"，真实含义是"本地台账
记了一笔"。这一层做的事很有限但很具体——**把业务系统的读收进一个接口，返回朴素的
值对象，不让任何 ORM 实体越过边界**。

为什么先只做读：**卡住真实集成的就是它**。写侧的 ``apply()`` 闭包现在握着
``CsOrder`` 这个 ORM 对象做状态机与余额校验，而一个 ERP 后端拿不到 session、也
构造不出这个类——所以只要 ORM 还在跨边界流动，写侧的接缝就立不起来。读侧先换成
值对象之后，写侧要接的接口形状就确定了（快照进、结果文本出），那一步变成机械活。

写侧**今天仍然直接改 ORM**（``tools.py`` 的 ``_must_find_order`` 与三个 ``apply()``），
接缝停在哪一行下面写着。不要把这当成"已经抽象完了"。

## 为什么值得为读单独开一层

不只是"以后好接 ERP"。这一层顺手解决了一个已经存在的问题：``cs_shipments`` 没有
``workspace_id`` 列，它的租户归属跟着订单走。原来按运单号查物流是一句
``filter(tracking_no == ...)`` 一把梭，于是任何工作区都能凭一个运单号看到别家的
承运商、状态与最新事件。隔离规则现在写在 ``LocalCsBusinessSystem`` 里面，
调用方拿不到"绕过 join 的裸查询"这个选项。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from models import CsCustomer, CsOrder, CsOrderItem, CsShipment

# 订单状态的全集。真源在这里，不在注释里：``models.py`` 的那行 docstring 列举过
# 同样的八个值，但它不会拒绝一个不存在的状态，而导入要判的就是"这个状态可能吗"。
#
# 新增一个状态时必须同时决定它落进下面哪一个集合，两边都有断言盯着：
# 漏出"在途"会让界面少算待办订单，漏出"已锁"会让退款绕过状态校验。
ORDER_STATUSES = frozenset(
    {
        "pending_payment",
        "paid",
        "packed",
        "shipped",
        "delivered",
        "cancelled",
        "refunding",
        "refunded",
    }
)

# 物流状态全集。``cs_shipments`` 没有 workspace 列，它的租户归属跟着订单走，
# 所以这个集合只用来挡"导入一个拼错的承运状态"，不参与隔离判断。
SHIPMENT_STATUSES = frozenset(
    {"pending", "in_transit", "out_for_delivery", "delivered", "exception", "returned"}
)

# 「在途订单」的口径。原来住在 tools.py 里，跟着读侧一起搬过来——这里刻意保持
# 逐字相同的四个值。"在途"少算一个状态，界面上就是"客户名下没有待办订单"，
# 而模型会据此决定要不要建议客户等一等。
_OPEN_ORDER_STATUSES = frozenset({"pending_payment", "paid", "packed", "refunding"})

# 「已经离开我们还能改那一段」的状态。它是**推出来的**，不是又抄了一遍：
# 全集减在途。这样新增状态时即便忘了分类，它默认落进"已锁"——对退款和改地址
# 而言，判成"不许动"是安全的一侧，判成"还能动"是要出钱的。
LOCKED_ORDER_STATUSES = ORDER_STATUSES - _OPEN_ORDER_STATUSES


@dataclass(frozen=True, slots=True)
class ShipmentSnapshot:
    carrier: str
    tracking_no: str
    status: str
    last_event: str | None


@dataclass(frozen=True, slots=True)
class OrderLine:
    title: str
    sku: str
    quantity: int
    unit_price: Decimal
    line_status: str | None


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    """一张订单在某一时刻的样子。

    字段集是照"读侧要渲染 + 写侧要校验"两处取并集定的：``status`` / ``total_amount``
    / ``refunded_amount`` / ``currency`` / ``customer_id`` 是退款与取消那两条写路径
    现在正在用的那几个，先把它们放进值对象里，接 ERP 时就不必再改一次接口。
    """

    id: str
    order_no: str
    status: str
    currency: str
    total_amount: Decimal
    refunded_amount: Decimal
    customer_id: str | None
    receiver_name: str | None
    receiver_phone: str | None
    address_text: str | None
    lines: tuple[OrderLine, ...] = ()
    shipments: tuple[ShipmentSnapshot, ...] = ()

    @property
    def refundable(self) -> Decimal:
        return self.total_amount - self.refunded_amount


@dataclass(frozen=True, slots=True)
class CustomerSnapshot:
    id: str
    display_name: str | None
    tier: str
    lifetime_amount: Decimal
    risk_flags: tuple[str, ...] = ()
    open_order_nos: tuple[str, ...] = ()


@runtime_checkable
class BusinessSystem(Protocol):
    """业务系统的读面。实现方负责租户隔离，调用方拿不到裸查询。"""

    def get_order(self, order_no: str) -> OrderSnapshot | None: ...

    def find_shipments_by_tracking(
        self, tracking_no: str
    ) -> tuple[ShipmentSnapshot, ...]: ...

    def find_customer(
        self, *, customer_id: str = "", email: str = "", phone: str = ""
    ) -> CustomerSnapshot | None: ...


class LocalCsBusinessSystem:
    """默认实现：同一套 ``cs_*`` 表。行为与直接 ORM 查询逐字一致。"""

    def __init__(self, db: Session, workspace_id: str) -> None:
        self._db = db
        self._workspace_id = workspace_id

    # ---- 读 -------------------------------------------------------------

    def get_order(self, order_no: str) -> OrderSnapshot | None:
        order = self._order_row(order_no)
        if order is None:
            return None
        return self._snapshot(order)

    def find_shipments_by_tracking(
        self, tracking_no: str
    ) -> tuple[ShipmentSnapshot, ...]:
        wanted = tracking_no.strip()
        if not wanted:
            return ()
        # join 回订单再按 workspace 筛：cs_shipments 没有 workspace_id 列。
        # 这条规则住在这里，是为了让"换个后端"和"忘了加隔离"不再长得一样。
        rows = (
            self._db.query(CsShipment)
            .join(CsOrder, CsShipment.order_id == CsOrder.id)
            .filter(
                CsShipment.tracking_no == wanted,
                CsOrder.workspace_id == self._workspace_id,
            )
            .all()
        )
        return tuple(_shipment_snapshot(row) for row in rows)

    def find_customer(
        self, *, customer_id: str = "", email: str = "", phone: str = ""
    ) -> CustomerSnapshot | None:
        customer = self._customer_row(customer_id=customer_id, email=email, phone=phone)
        if customer is None:
            return None
        open_orders = (
            self._db.query(CsOrder)
            .filter(
                CsOrder.workspace_id == self._workspace_id,
                CsOrder.customer_id == customer.id,
                CsOrder.status.in_(_OPEN_ORDER_STATUSES),
            )
            .all()
        )
        return CustomerSnapshot(
            id=customer.id,
            display_name=customer.display_name,
            tier=customer.tier,
            lifetime_amount=customer.lifetime_amount,
            risk_flags=_risk_flags(customer.risk_flags),
            open_order_nos=tuple(order.order_no for order in open_orders),
        )

    # ---- 内部：ORM 不越界 ----------------------------------------------

    def _order_row(self, order_no: str) -> CsOrder | None:
        return (
            self._db.query(CsOrder)
            .filter(
                CsOrder.workspace_id == self._workspace_id,
                CsOrder.order_no == (order_no or "").strip(),
            )
            .first()
        )

    def _customer_row(
        self, *, customer_id: str, email: str, phone: str
    ) -> CsCustomer | None:
        query = self._db.query(CsCustomer).filter(
            CsCustomer.workspace_id == self._workspace_id
        )
        if customer_id.strip():
            return query.filter(CsCustomer.id == customer_id.strip()).first()
        normalized = (email or "").strip().lower()
        if normalized:
            return query.filter(CsCustomer.email == normalized).first()
        digits = "".join(character for character in (phone or "") if character.isdigit())
        if digits:
            return query.filter(CsCustomer.phone == digits).first()
        return None

    def _snapshot(self, order: CsOrder) -> OrderSnapshot:
        items = self._db.query(CsOrderItem).filter(CsOrderItem.order_id == order.id).all()
        shipments = self._db.query(CsShipment).filter(CsShipment.order_id == order.id).all()
        return OrderSnapshot(
            id=order.id,
            order_no=order.order_no,
            status=order.status,
            currency=order.currency,
            total_amount=order.total_amount,
            refunded_amount=order.refunded_amount,
            customer_id=order.customer_id,
            receiver_name=order.receiver_name,
            receiver_phone=order.receiver_phone,
            address_text=order.address_text,
            lines=tuple(_line(item) for item in items),
            shipments=tuple(_shipment_snapshot(row) for row in shipments),
        )


def _shipment_snapshot(row: CsShipment) -> ShipmentSnapshot:
    return ShipmentSnapshot(
        carrier=row.carrier,
        tracking_no=row.tracking_no,
        status=row.status,
        last_event=row.last_event,
    )


def _line(item: CsOrderItem) -> OrderLine:
    return OrderLine(
        title=item.title,
        sku=item.sku,
        quantity=item.quantity,
        unit_price=item.unit_price,
        line_status=item.line_status,
    )


def _risk_flags(raw: str | None) -> tuple[str, ...]:
    """风险标记存的是 JSON 文本。坏掉就当没有——它不该让整次客户查询失败。"""
    if not raw:
        return ()
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(str(flag) for flag in loaded)


def for_session(db: Session, workspace_id: str) -> BusinessSystem:
    """按配置挑实现。

    只有一个实现可选的现在，这个函数看起来是多余的。它是留给 ERP 那一侧的接缝：
    接上之后换实现的判断只出现在这一处，而不是散落在每个工具函数里。
    """
    return LocalCsBusinessSystem(db, workspace_id)
