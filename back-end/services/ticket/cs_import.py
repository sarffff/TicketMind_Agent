"""业务数据入口：让 ``cs_*`` 这几张表真的能有数据。

这一层存在的理由很直接：在把 ERP 接进来之前，本系统里**没有任何一段生产代码会
创建订单**（``CsOrder`` 只在测试和离线评估里被构造过）。于是一个刚装好的实例，
每一张工单走到 ``lookup_order`` 都是"查无此单"——自动解决率、退款闸门、幂等台账
全都接不上电，能演示的只有评估里那份自造数据。

## 三条边界

**一、租户不从数据里来。** ``workspace_id`` 只认调用方（已鉴权的管理员）那一份，
载荷里出现这个字段就整批拒绝。静默忽略比报错更糟：运营以为导进了 A 工作区，
实际落在自己的 B 里，之后查不到还以为是导入失败。

**二、只写业务表，绝不碰台账。** 当日退款额度聚合的是 ``cs_operations``
里 ``permission='fund'`` 的行（``governor.refund_used_today``）。导入要是顺手写几笔
历史退款进那张表，**一批迁移数据会当场吃掉今天的退款额度**，症状是导入之后所有
退款都被判"额度不足"转人工——看起来像治理生效了，其实是账被污染了。
``cs_orders.refunded_amount`` 那一列则是安全的：它是订单上的既成事实，只减小
可退余额，不进额度账。

**三、不覆写画像。** ``cs_customers.profile`` 是跨工单累积出来的结论（偏好、
历史处置、风险注意点）。导入的文件里没有资格改写它——那会把系统已经学到的东西
抹掉。这里只动 ``display_name`` / ``tier`` / ``lifetime_amount`` / ``risk_flags``
这类"ERP 才是权威"的字段。

## 为什么全有或全无

任意一行校验不过就整批不写。半套数据的破坏性是具体的：订单头落了而明细没落，
模型读到的是"总额 0 但有商品"，它接下来给出的建议建立在自相矛盾的事实上，
而这种矛盾在轨迹里看起来完全正常。因为导入是幂等的（按业务键 upsert），
"改好文件再跑一遍"就是完整恢复路径，没有部分成功的必要。

## 幂等靠业务键，不靠调用方传键

订单认 ``(workspace_id, order_no)``，行认 ``(order_id, sku)``，发货认
``(order_id, tracking_no)``，客户认归一化后的 email / phone / external_ref。
**刻意不要求调用方提供幂等键**：ERP 导出不会有它，而"客户口中报出来的那个号"
本来就是这一层的业务主键。同一份文件跑两遍得到同一个状态。

更新**只增改、从不删**。文件里少了一行不代表订单上少了一件商品——那更可能是
导出漏了。删除需要明确的操作，不靠"这次没提到"来推断。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from models import CsCustomer, CsOrder, CsOrderItem, CsShipment
from services.clock import naive_now
from services.ticket.business_system import ORDER_STATUSES, SHIPMENT_STATUSES
from services.ticket.intake import normalize_email, normalize_phone

# 一次请求的上限。这个端点会逐行建 ORM 对象并 flush，没有上限的话它就是一句
# "给我一台机器的 root 权限就能把库撑满"
MAX_CUSTOMERS = 500
MAX_ORDERS = 500
MAX_ITEMS_PER_ORDER = 100
MAX_SHIPMENTS_PER_ORDER = 20

# Numeric(12,2) 的上界。越过它 MySQL 在非严格模式下静默截断成 9999999999.99，
# 于是"这单能退多少"从此和来源系统差一个数量级，而且没有任何一处会报错
MAX_MONEY = Decimal("9999999999.99")

# 载荷里不许出现的键。租户归属由调用方决定，见模块文档
_FORBIDDEN_KEYS = frozenset({"workspace_id", "tenant_id", "id"})

_TOP_LEVEL_KEYS = frozenset({"customers", "orders"})
_CUSTOMER_KEYS = frozenset(
    {"external_ref", "email", "phone", "display_name", "tier", "lifetime_amount", "risk_flags"}
)
_ORDER_KEYS = frozenset(
    {
        "order_no",
        "status",
        "currency",
        "total_amount",
        "refunded_amount",
        "receiver_name",
        "receiver_phone",
        "address_text",
        "created_at",
        "customer",
        "items",
        "shipments",
    }
)
_CUSTOMER_REF_KEYS = frozenset({"external_ref", "email", "phone"})
_ITEM_KEYS = frozenset({"sku", "title", "quantity", "unit_price", "line_status"})
_SHIPMENT_KEYS = frozenset(
    {"carrier", "tracking_no", "status", "last_event", "last_event_at"}
)


@dataclass(frozen=True, slots=True)
class Rejected:
    """一行没过的原因。``ref`` 是运营在自己文件里能找到的那个东西——订单号、
    邮箱，或者行号，不是我们的内部 id。"""

    kind: str
    ref: str
    reason: str


@dataclass(frozen=True, slots=True)
class ImportResult:
    customers_created: int = 0
    customers_updated: int = 0
    orders_created: int = 0
    orders_updated: int = 0
    items_written: int = 0
    shipments_written: int = 0
    rejected: tuple[Rejected, ...] = ()

    @property
    def applied(self) -> bool:
        return not self.rejected

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "customersCreated": self.customers_created,
            "customersUpdated": self.customers_updated,
            "ordersCreated": self.orders_created,
            "ordersUpdated": self.orders_updated,
            "itemsWritten": self.items_written,
            "shipmentsWritten": self.shipments_written,
            "rejected": [
                {"kind": r.kind, "ref": r.ref, "reason": r.reason} for r in self.rejected
            ],
        }


class _RowError(Exception):
    """这一行不合格。带着要回给运营的那句话。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- 标量校验


def _money(value: Any, *, field: str, default: Decimal | None = None) -> Decimal:
    """金额。只接受字符串与整数/浮点，一律先 ``str`` 再进 ``Decimal``。

    ``Decimal(128.5)`` 拿到的是 128.500000000000005684…，这是二进制浮点的本来
    面目；而这里算的是"还能退多少"，差一分都会在退款校验上被判成不同结论。
    """
    if value is None:
        if default is None:
            raise _RowError(f"{field} 缺失")
        return default
    if isinstance(value, bool):  # bool 是 int 的子类，True 会被当成 1 块钱
        raise _RowError(f"{field} 不是金额（{value!r}）")
    if not isinstance(value, (str, int, float, Decimal)):
        raise _RowError(f"{field} 不是金额（{type(value).__name__}）")
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise _RowError(f"{field} 解析不出金额：{value!r}") from None
    if not amount.is_finite():  # Decimal("nan") / "Infinity" 都能构造成功
        raise _RowError(f"{field} 不是有限金额：{value!r}")
    if amount < 0:
        raise _RowError(f"{field} 不能为负：{amount}")
    if amount > MAX_MONEY:
        raise _RowError(f"{field} 超出列容量 {MAX_MONEY}：{amount}")
    return amount.quantize(Decimal("0.01"))


def _text(
    value: Any, *, field: str, limit: int, required: bool = False, default: str | None = None
) -> str | None:
    if value is None:
        if required:
            raise _RowError(f"{field} 必填")
        return default
    if not isinstance(value, str):
        raise _RowError(f"{field} 要的是文本，拿到 {type(value).__name__}")
    cleaned = value.strip()
    if not cleaned:
        if required:
            raise _RowError(f"{field} 必填")
        return default
    if len(cleaned) > limit:
        raise _RowError(f"{field} 超过 {limit} 字（{len(cleaned)} 字）")
    return cleaned


def _moment(value: Any, *, field: str, now: datetime) -> datetime:
    """时间戳。列是 DateTime（无时区），所以带偏移的输入要折算成 naive。"""
    if value is None:
        return now
    if not isinstance(value, str):
        raise _RowError(f"{field} 要的是 ISO 8601 文本，拿到 {type(value).__name__}")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _RowError(f"{field} 不是合法时间：{raw!r}") from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    if parsed > now:
        # 未来的订单会躲开所有"最近 N 天"的口径，还会让当日额度的自然日边界失真
        raise _RowError(f"{field} 在未来：{raw}")
    return parsed


def _enum(value: Any, allowed: frozenset[str], *, field: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise _RowError(f"{field} 要的是文本")
    cleaned = value.strip()
    if cleaned not in allowed:
        raise _RowError(f"{field} 不认识：{cleaned!r}（可用 {'/'.join(sorted(allowed))}）")
    return cleaned


def _unknown_keys(row: Any, allowed: frozenset[str], *, where: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise _RowError(f"{where} 要的是一个对象")
    extra = set(row) - allowed
    if extra:
        raise _RowError(
            f"{where} 有它不认的字段：{'、'.join(sorted(extra))}"
            "（拼错的字段会被静默丢弃，那比报错更难查）"
        )
    banned = set(row) & _FORBIDDEN_KEYS
    if banned:
        raise _RowError(
            f"{where} 不许带 {'、'.join(sorted(banned))}：属于哪个工作区由调用方的身份决定，"
            "不从数据里读"
        )
    return row


def _risk_flags(value: Any, *, where: str) -> str | None:
    """风险标记存成 json 数组文本。理解层与 ``lookup_customer`` 都按数组读，
    所以这里不能放进去一个字符串了事。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise _RowError(f"{where} 的 risk_flags 要的是字符串数组")
    flags = [x.strip() for x in value if isinstance(x, str) and x.strip()]
    if len(flags) > 20:
        raise _RowError(f"{where} 的 risk_flags 太多了（{len(flags)} 条）")
    return json.dumps(flags, ensure_ascii=False) if flags else None


# ---------------------------------------------------------------- 行内结构


@dataclass(slots=True)
class _Customer:
    ref: str
    external_ref: str | None
    email: str | None
    phone: str | None
    display_name: str | None
    tier: str | None
    # None 说的是"文件里没提"，不是"零"。这两者在导入语义上完全不同：
    # 前者不许动库里已有的值
    lifetime_amount: Decimal | None
    risk_flags: str | None
    row: CsCustomer | None = None


@dataclass(slots=True)
class _Item:
    sku: str
    title: str
    quantity: int
    unit_price: Decimal
    line_status: str | None


@dataclass(slots=True)
class _Shipment:
    carrier: str
    tracking_no: str
    status: str
    last_event: str | None
    last_event_at: datetime | None


@dataclass(slots=True)
class _Order:
    ref: str
    order_no: str
    status: str
    currency: str
    total_amount: Decimal
    refunded_amount: Decimal
    receiver_name: str | None
    receiver_phone: str | None
    address_text: str | None
    created_at: datetime
    has_created_at: bool
    customer: _Customer | None
    items: tuple[_Item, ...]
    shipments: tuple[_Shipment, ...]
    row: CsOrder | None = None


def _parse_customer(row: Any) -> _Customer:
    data = _unknown_keys(row, _CUSTOMER_KEYS, where="客户行")
    email = normalize_email(_text(data.get("email"), field="email", limit=255))
    phone = normalize_phone(_text(data.get("phone"), field="phone", limit=32))
    external_ref = _text(data.get("external_ref"), field="external_ref", limit=120)
    if not (email or phone or external_ref):
        raise _RowError("email / phone / external_ref 至少要有一个，否则这一行没有身份")
    lifetime = data.get("lifetime_amount")
    return _Customer(
        ref="",  # 由收集器统一填，见 _collect
        external_ref=external_ref,
        email=email,
        phone=phone,
        display_name=_text(data.get("display_name"), field="display_name", limit=120),
        tier=_text(data.get("tier"), field="tier", limit=20),
        lifetime_amount=None
        if lifetime is None
        else _money(lifetime, field="lifetime_amount"),
        risk_flags=_risk_flags(data.get("risk_flags"), where="客户行"),
    )


def _parse_order(row: Any, *, now: datetime) -> _Order:
    data = _unknown_keys(row, _ORDER_KEYS, where="订单行")
    order_no = _text(data.get("order_no"), field="order_no", limit=40, required=True)
    total = _money(data.get("total_amount"), field="total_amount")
    refunded = _money(data.get("refunded_amount"), field="refunded_amount", default=Decimal("0"))
    if refunded > total:
        # 这一列减出来的是"还能退多少"。负的 refundable 会让超额校验整个失效：
        # ``amount > refundable`` 对任何正数都成立，于是每一笔退款都"超额"
        raise _RowError(f"refunded_amount({refunded}) 大于 total_amount({total})，可退余额会是负数")

    customer = data.get("customer")
    if customer is not None:
        customer = _unknown_keys(customer, _CUSTOMER_REF_KEYS, where="订单行的 customer")
        if not any((customer.get("email"), customer.get("phone"), customer.get("external_ref"))):
            raise _RowError("customer 要么整个不给，要么至少给一个标识字段")

    items = tuple(
        _parse_item(item, index=i) for i, item in enumerate(data.get("items") or [])
    )
    if len(items) > MAX_ITEMS_PER_ORDER:
        raise _RowError(f"一张单最多 {MAX_ITEMS_PER_ORDER} 行商品，收到 {len(items)}")
    shipments = tuple(
        _parse_shipment(x, index=i, now=now) for i, x in enumerate(data.get("shipments") or [])
    )
    if len(shipments) > MAX_SHIPMENTS_PER_ORDER:
        raise _RowError(
            f"一张单最多 {MAX_SHIPMENTS_PER_ORDER} 个包裹，收到 {len(shipments)}"
        )

    order = _Order(
        ref="",
        order_no=order_no,
        status=_enum(data.get("status"), ORDER_STATUSES, field="status", default="paid"),
        currency=(_text(data.get("currency"), field="currency", limit=8) or "CNY").upper(),
        total_amount=total,
        refunded_amount=refunded,
        receiver_name=_text(data.get("receiver_name"), field="receiver_name", limit=60),
        receiver_phone=normalize_phone(
            _text(data.get("receiver_phone"), field="receiver_phone", limit=32)
        ),
        address_text=_text(data.get("address_text"), field="address_text", limit=500),
        created_at=_moment(data.get("created_at"), field="created_at", now=now),
        has_created_at=data.get("created_at") is not None,
        customer=None
        if customer is None
        else _Customer(
            ref="订单里的 customer",
            external_ref=_text(
                customer.get("external_ref"), field="customer.external_ref", limit=120
            ),
            email=normalize_email(
                _text(customer.get("email"), field="customer.email", limit=255)
            ),
            phone=normalize_phone(
                _text(customer.get("phone"), field="customer.phone", limit=32)
            ),
            display_name=None,
            tier=None,
            lifetime_amount=None,
            risk_flags=None,
        ),
        items=items,
        shipments=shipments,
    )
    return order


def _parse_item(row: Any, *, index: int) -> _Item:
    data = _unknown_keys(row, _ITEM_KEYS, where="商品行")
    quantity = data.get("quantity", 1)
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise _RowError(f"items[{index}].quantity 要的是整数")
    if quantity < 1:
        raise _RowError(f"items[{index}].quantity 至少 1，收到 {quantity}")
    if quantity > 1_000_000:
        raise _RowError(f"items[{index}].quantity 大得不像真数据：{quantity}")
    return _Item(
        sku=_text(data.get("sku"), field="sku", limit=64, required=True),
        title=_text(data.get("title"), field="title", limit=255, required=True),
        quantity=quantity,
        unit_price=_money(data.get("unit_price"), field="unit_price"),
        line_status=_text(data.get("line_status"), field="line_status", limit=20),
    )


def _parse_shipment(row: Any, *, index: int, now: datetime) -> _Shipment:
    data = _unknown_keys(row, _SHIPMENT_KEYS, where="发货行")
    event_at = data.get("last_event_at")
    return _Shipment(
        carrier=_text(data.get("carrier"), field="carrier", limit=40, required=True),
        tracking_no=_text(data.get("tracking_no"), field="tracking_no", limit=64, required=True),
        status=_enum(
            data.get("status"), SHIPMENT_STATUSES, field="shipments 的 status", default="in_transit"
        ),
        last_event=_text(data.get("last_event"), field="last_event", limit=500),
        last_event_at=None if event_at is None else _moment(
            event_at, field="last_event_at", now=now
        ),
    )


def _collect(rows: list[Any], parse: Any, *, prefix: str, kind: str) -> tuple[list[Any], list[Rejected]]:
    """逐行解析，把**所有**错误收齐再回。

    在第一处就抛出对运营没用了：改文件的人本来就要重跑一遍，一次只告诉他一行坏了
    意味着改一行跑一次。这里全收，写库与否仍然由调用方一次性决定。
    """
    parsed: list[Any] = []
    rejected: list[Rejected] = []
    for index, raw in enumerate(rows):
        ref = f"{prefix}[{index}]"
        try:
            item = parse(raw)
        except _RowError as exc:
            rejected.append(Rejected(kind=kind, ref=ref, reason=exc.reason))
            continue
        item.ref = ref
        parsed.append(item)
    return parsed, rejected


# ---------------------------------------------------------------- 主入口


def import_cs_data(
    db: Session, workspace_id: str, payload: dict[str, Any], *, now: datetime | None = None
) -> ImportResult:
    """校验并落库。**只 flush，不 commit**——调用方决定这笔导入和它的审计要不要
    一起提交；``audit_log.record`` 自己会 commit，先记审计会把半截导入持久化。"""
    stamp = now or naive_now()
    if not isinstance(payload, dict):
        return _refused("载荷要是一个 JSON 对象")

    extra = set(payload) - _TOP_LEVEL_KEYS
    if extra:
        return _refused(
            f"顶层有它不认的字段：{'、'.join(sorted(extra))}（只接受 customers / orders）"
        )

    customer_rows = payload.get("customers") or []
    order_rows = payload.get("orders") or []
    if not isinstance(customer_rows, list) or not isinstance(order_rows, list):
        return _refused("customers / orders 要都是数组")
    if not customer_rows and not order_rows:
        return _refused("没有要导入的东西：两份数组都是空的")
    if len(customer_rows) > MAX_CUSTOMERS:
        return _refused(f"一次最多导入 {MAX_CUSTOMERS} 个客户，收到 {len(customer_rows)}")
    if len(order_rows) > MAX_ORDERS:
        return _refused(f"一次最多导入 {MAX_ORDERS} 张订单，收到 {len(order_rows)}")

    customers, rejected = _collect(customer_rows, _parse_customer, prefix="customers", kind="customer")
    orders, order_errors = _collect(
        order_rows, lambda row: _parse_order(row, now=stamp), prefix="orders", kind="order"
    )
    rejected.extend(order_errors)
    if rejected:
        return ImportResult(rejected=tuple(rejected))

    # 第一阶段后半：跨行与对库的校验。仍然是只读
    rejected = _cross_check(db, workspace_id, customers, orders, stamp)
    if rejected:
        return ImportResult(rejected=tuple(rejected))

    return _write(db, workspace_id, customers, orders, stamp)


def _refused(reason: str) -> ImportResult:
    return ImportResult(rejected=(Rejected(kind="payload", ref="-", reason=reason),))


# ---------------------------------------------------------------- 跨行校验


def _identity_keys(c: _Customer) -> dict[str, str]:
    """归一化之后的身份键。刻意复用 ``intake`` 的那两个归一化函数：同一个人因为
    写了 ``+86`` 而被当成两个人，画像和限额都会分裂（``models.py`` 的客户表注释
    写的就是这件事）。"""
    keys: dict[str, str] = {}
    if c.email:
        keys["email"] = c.email
    if c.phone:
        keys["phone"] = c.phone
    if c.external_ref:
        keys["external_ref"] = c.external_ref
    return keys


def _find_customer_row(db: Session, workspace_id: str, c: _Customer) -> CsCustomer | None:
    for field, value in _identity_keys(c).items():
        row = (
            db.query(CsCustomer)
            .filter(CsCustomer.workspace_id == workspace_id, getattr(CsCustomer, field) == value)
            .first()
        )
        if row is not None:
            return row
    return None


def _cross_check(
    db: Session,
    workspace_id: str,
    customers: list[_Customer],
    orders: list[_Order],
    stamp: datetime,
) -> list[Rejected]:
    rejected: list[Rejected] = []

    # 同一份文件里两个人抢同一个身份：落库顺序决定谁赢，而那没人会去看
    seen: dict[tuple[str, str], str] = {}
    for c in customers:
        for field, value in _identity_keys(c).items():
            holder = seen.get((field, value))
            if holder is not None:
                rejected.append(
                    Rejected(
                        kind="customer",
                        ref=c.ref,
                        reason=f"这一行的 {field} 和 {holder} 撞了，一批里两个人抢同一个身份",
                    )
                )
            else:
                seen[(field, value)] = c.ref
        c.row = _find_customer_row(db, workspace_id, c)

    by_no: dict[str, str] = {}
    for order in orders:
        holder = by_no.get(order.order_no)
        if holder is not None:
            rejected.append(
                Rejected(
                    kind="order",
                    ref=order.ref,
                    reason=f"order_no {order.order_no} 在这一批里出现了两次（{holder} 也是它）",
                )
            )
        else:
            by_no[order.order_no] = order.ref

        if order.customer is not None:
            # 先看是不是同一份文件里正在新建的那个客户，否则回库查
            inline = next(
                (
                    x
                    for x in customers
                    if set(_identity_keys(x).items()) & set(_identity_keys(order.customer).items())
                ),
                None,
            )
            order.customer = inline or order.customer
            if inline is None:
                order.customer.row = _find_customer_row(db, workspace_id, order.customer)
                if order.customer.row is None:
                    rejected.append(
                        Rejected(
                            kind="order",
                            ref=order.ref,
                            reason=(
                                "customer 指的是库里没有的人，这一批的 customers 里也没有它。"
                                "这里不自动建客户档案：邮箱打错一个字就会多出一个幽灵客户，"
                                "而这个人的画像与限额从此被劈成两半"
                            ),
                        )
                    )

        order.row = (
            db.query(CsOrder)
            .filter(CsOrder.workspace_id == workspace_id, CsOrder.order_no == order.order_no)
            .first()
        )
        if order.row is not None and order.row.customer_id and order.customer is not None:
            resolved = order.customer.row
            if resolved is not None and resolved.id != order.row.customer_id:
                rejected.append(
                    Rejected(
                        kind="order",
                        ref=order.ref,
                        reason=(
                            f"订单 {order.order_no} 已经挂在另一个客户名下。把单子在两个客户之间"
                            "挪动会连带挪走退款资格，这不是导入该有的副作用"
                        ),
                    )
                )
    return rejected


# ---------------------------------------------------------------- 落库


def _write(
    db: Session,
    workspace_id: str,
    customers: list[_Customer],
    orders: list[_Order],
    stamp: datetime,
) -> ImportResult:
    result_created = result_updated = 0

    for c in customers:
        if c.row is None:
            c.row = CsCustomer(
                workspace_id=workspace_id,
                external_ref=c.external_ref,
                email=c.email,
                phone=c.phone,
                display_name=c.display_name,
                tier=c.tier or "standard",
                lifetime_amount=c.lifetime_amount if c.lifetime_amount is not None else Decimal("0"),
                risk_flags=c.risk_flags,
                profile=None,
                profile_version=0,
                created_at=stamp,
                updated_at=stamp,
            )
            db.add(c.row)
            result_created += 1
        else:
            row = c.row
            # 文件没提的字段一律不动。ERP 导出少带一列是常事，那一列不该被抹成空
            if c.display_name is not None:
                row.display_name = c.display_name
            if c.tier is not None:
                row.tier = c.tier
            if c.lifetime_amount is not None:
                row.lifetime_amount = c.lifetime_amount
            if c.risk_flags is not None:
                row.risk_flags = c.risk_flags
            # profile / profile_version 不在这里：那是系统跨工单学出来的结论，
            # 外部文件没有资格改写它，见模块文档
            row.updated_at = stamp
            result_updated += 1
    db.flush()

    orders_created = orders_updated = items_written = shipments_written = 0

    for order in orders:
        customer_id = order.customer.row.id if order.customer and order.customer.row else None
        if order.row is None:
            order.row = CsOrder(
                workspace_id=workspace_id,
                customer_id=customer_id,
                order_no=order.order_no,
                status=order.status,
                currency=order.currency,
                total_amount=order.total_amount,
                refunded_amount=order.refunded_amount,
                receiver_name=order.receiver_name,
                receiver_phone=order.receiver_phone,
                address_text=order.address_text,
                created_at=order.created_at,
                updated_at=stamp,
            )
            db.add(order.row)
            orders_created += 1
        else:
            row = order.row
            row.status = order.status
            row.currency = order.currency
            row.total_amount = order.total_amount
            row.refunded_amount = order.refunded_amount
            if customer_id and not row.customer_id:
                # 只在"原来没主人"的时候补上；换主人已经在跨行校验里拒了
                row.customer_id = customer_id
            for field, value in (
                ("receiver_name", order.receiver_name),
                ("receiver_phone", order.receiver_phone),
                ("address_text", order.address_text),
            ):
                if value is not None:
                    setattr(row, field, value)
            if order.has_created_at:
                row.created_at = order.created_at
            row.updated_at = stamp
            orders_updated += 1
        db.flush()

        items_written += _merge_items(db, order)
        shipments_written += _merge_shipments(db, order)

    return ImportResult(
        customers_created=result_created,
        customers_updated=result_updated,
        orders_created=orders_created,
        orders_updated=orders_updated,
        items_written=items_written,
        shipments_written=shipments_written,
    )


def _merge_items(db: Session, order: _Order) -> int:
    """按 sku 合行。**从不删除**：文件里少一行不代表订单上少一件商品，那更可能是
    导出漏了，而"这次没提到"不该被推断成删除。"""
    existing = {
        item.sku: item
        for item in db.query(CsOrderItem).filter(CsOrderItem.order_id == order.row.id).all()
    }
    written = 0
    for line in order.items:
        row = existing.get(line.sku)
        if row is None:
            db.add(
                CsOrderItem(
                    order_id=order.row.id,
                    sku=line.sku,
                    title=line.title,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    line_status=line.line_status,
                    created_at=order.created_at,
                )
            )
        else:
            row.title = line.title
            row.quantity = line.quantity
            row.unit_price = line.unit_price
            if line.line_status is not None:
                row.line_status = line.line_status
        written += 1
    db.flush()
    return written


def _merge_shipments(db: Session, order: _Order) -> int:
    existing = {
        row.tracking_no: row
        for row in db.query(CsShipment).filter(CsShipment.order_id == order.row.id).all()
    }
    written = 0
    for shipment in order.shipments:
        row = existing.get(shipment.tracking_no)
        if row is None:
            db.add(
                CsShipment(
                    order_id=order.row.id,
                    carrier=shipment.carrier,
                    tracking_no=shipment.tracking_no,
                    status=shipment.status,
                    last_event=shipment.last_event,
                    last_event_at=shipment.last_event_at,
                    created_at=order.created_at,
                    updated_at=naive_now(),
                )
            )
        else:
            row.carrier = shipment.carrier
            row.status = shipment.status
            if shipment.last_event is not None:
                row.last_event = shipment.last_event
            if shipment.last_event_at is not None:
                row.last_event_at = shipment.last_event_at
            row.updated_at = naive_now()
        written += 1
    db.flush()
    return written


