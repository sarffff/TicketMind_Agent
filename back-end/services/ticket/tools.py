"""能力层：业务系统的工具面。

对应文档的第 3 层（MCP Servers + Skills）与第 6.1 节的权限分级。真实部署里这些
工具是订单中心 / CRM / 支付网关 / 物流平台，经 MCP 暴露；这里先落在本仓库自建的
靶子表上（``models`` 里 ``Cs*`` 那一组），理由是文档 Phase 1–3 要求的不是接真系统，
而是**让 Agent 的写操作有对象可写、并且每一次写都能被断言**。接真系统时该换掉的
只是每个 ``*_...`` 函数体内的 ORM 调用，工具契约、幂等与权限档位都不动。

## 三层校验各管一件事

``ToolRuntime._validate`` 管 JSON Schema（缺字段、类型错、多余键）；这里每个工具的
``Args`` 模型管业务约束（金额必须为正、查询条件至少给一个、枚举取值）；再往下是
函数体里的**状态机校验**（已经发货的单子不能改地址）。Schema 由 Pydantic 模型
生成而不是另写一份：两份定义迟早对不上，而对不上的那次是模型收到一个永远校验
不过的工具。

## 幂等键不需要模型发明

见 ``ledger.derive_key``：按 ``(工单, 操作, 参数)`` 推出来。让模型自己编键等于让它
每次编一个不同的随机串，那种键的幂等性是零。显式传入的键优先，那是留给
"人把参数改了之后重新发起"的口子。

## 业务失败是一条异常，不是一段文本

``BusinessReject`` 表示"这次动作不成立"（查无此单、已发货改不了、超出可退余额），
它会原样穿过函数层，在 handler 边界被翻成回灌给客户模型的那句中文。为什么不用
返回文本来表达：抛异常会被 ``ToolRuntime`` 归成 ``unavailable``（"这个工具坏了，
别再试"），而"查无此单"恰恰相反——工具好得很，是参数或事实不对，模型下一步该做的
是换个订单号或者回头问客户。处置相反的两件事必须走不同的通道，所以真故障留给
熔断器数，业务拒绝走 ``BusinessReject``，而治理拦下（暂停、超额）是第三种：
它是一条**可以照着做的指示**，于是返回文本并记 ``blocked``。
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from models import CsInvoice, CsOperation, CsOrder, CsRefund
from services import audit_log
from services.clock import naive_now
from services.ticket import business_system
from services.ticket import governor, ledger
from services.ticket.business_system import OrderSnapshot, ShipmentSnapshot
from services.ticket.understand import parse_amount
from services.tool_runtime import ToolDefinition

# 业务系统的读全部走这一处。名字短是有意的：它在每个查询工具里都要出现，
# 而"忘了走边界、直接 query 一张 cs_* 表"是这一层最容易悄悄退回去的地方。
_system = business_system.for_session

READ = "read"
MUTATE = "mutate"
FUND = "fund"

# 已经离开"我们还能改"那一段的状态。口径住在 business_system（它同时给导入校验
# 用），这里只是引用——两张清单各写一份的话，漂移的方式是"导入接受了一个状态而
# 写路径不认"，症状是数据进得来、动不了。
_LOCKED_ORDER_STATUSES = business_system.LOCKED_ORDER_STATUSES
# "在途订单"的口径不在这里了——它跟着读侧搬进了 business_system。
# 留两份相等清单正是本仓库反复栽的那个形状：一开始相等，后来才漂移。


class BusinessReject(Exception):
    """业务上不允许这么做，但工具本身好好的。带上要回灌给客户看的那句话。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# 参数契约
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    """所有工具参数的基类：多余键一律拒绝。

    ``extra="forbid"`` 让 ``additionalProperties: false`` 出现在生成的 schema 里，
    运行时那一层也跟着拦。模型多写一个字段通常说明它把这个工具和别的搞混了，
    静默收下等于帮它继续搞下去。
    """

    model_config = ConfigDict(extra="forbid")


class LookupOrderArgs(_Args):
    order_no: str = Field(description="客户报出来的订单号，原样照抄")


class LookupLogisticsArgs(_Args):
    order_no: str = Field(default="", description="订单号，和运单号至少给一个")
    tracking_no: str = Field(default="", description="运单号，和订单号至少给一个")

    @model_validator(mode="after")
    def _at_least_one(self):
        if not self.order_no.strip() and not self.tracking_no.strip():
            raise ValueError("order_no 与 tracking_no 至少要给一个")
        return self


class LookupCustomerArgs(_Args):
    customer_id: str = Field(default="", description="客户档案 ID")
    email: str = Field(default="", description="客户邮箱")
    phone: str = Field(default="", description="客户手机号")

    @model_validator(mode="after")
    def _at_least_one(self):
        if not any(value.strip() for value in (self.customer_id, self.email, self.phone)):
            raise ValueError("customer_id / email / phone 至少要给一个")
        return self


class UpdateAddressArgs(_Args):
    order_no: str = Field(description="要改收货信息的订单号")
    receiver_name: str = Field(default="", description="新收件人姓名，不改就留空")
    receiver_phone: str = Field(default="", description="新收件人电话，不改就留空")
    address_text: str = Field(default="", description="新的完整收货地址，不改就留空")
    idempotency_key: str = Field(default="", description="重复提交同一笔改动时传同一个键")

    @model_validator(mode="after")
    def _at_least_one(self):
        if not any(
            value.strip()
            for value in (self.receiver_name, self.receiver_phone, self.address_text)
        ):
            raise ValueError("收件人、电话、地址至少要改一项")
        return self


class RequestInvoiceArgs(_Args):
    order_no: str = Field(description="要开票的订单号")
    title: str = Field(description="发票抬头")
    tax_no: str = Field(default="", description="购方纳税人识别号，个人抬头留空")
    email: str = Field(default="", description="接收电子发票的邮箱")
    idempotency_key: str = Field(default="", description="重复提交同一张发票申请时传同一个键")


class CreateRefundArgs(_Args):
    order_no: str = Field(description="要退款的订单号")
    # 金额是字符串而不是 number：JSON 数字走 float，而 float 在钱的算术上迟早咬人。
    # Decimal 从字符串来才是准的。
    amount: str = Field(description="退款金额，例如 350 或 1,200")
    reason_code: str = Field(default="", description="退款原因，例如 quality / delay / unwanted")
    idempotency_key: str = Field(default="", description="重复提交同一笔退款时传同一个键")


class CancelOrderArgs(_Args):
    order_no: str = Field(description="要取消的订单号")
    reason: str = Field(default="", description="客户说的取消原因")
    idempotency_key: str = Field(default="", description="重复提交同一笔取消时传同一个键")


# ---------------------------------------------------------------------------
# 权限档位
# ---------------------------------------------------------------------------

# 工具名 → 权限档位。这张表是治理唯一的输入：白名单按它筛、账本按它记、审批闸门
# 按它决定要不要挂起。"这个工具有多大权力"必须只有一个答案，所以它既不散在
# 各个 build 分支里，也不在别处再维护一份名单。
TIER_BY_TOOL: dict[str, str] = {
    "lookup_order": READ,
    "lookup_logistics": READ,
    "lookup_customer": READ,
    "update_order_address": MUTATE,
    "request_invoice": MUTATE,
    "create_refund": FUND,
    "cancel_order": FUND,
}

# 执行前必须有人点头的工具。判据就是"是不是资金类"这一条，没有第二套名单。
APPROVAL_TOOLS = frozenset(name for name, tier in TIER_BY_TOOL.items() if tier == FUND)

_TIER_FLOOR = {READ: 0, MUTATE: 1, FUND: 2}
_RISK_FLOOR = {"low": 0, "mid": 1, "high": 2}

# 编排层往工具面上追加的**非业务**工具：SOP 加载、政策检索、委派。
# 它们不进 ``TIER_BY_TOOL``，因为那张表同时是 ``build()`` 装配业务工具的清单来源
# （按档位筛、逐个配 handler），而这三个的 handler 在编排层手里。
# 但"这次调用要不要人批"必须有一个答案，所以在这里给它们一个档位：一律 READ——
# 它们不碰业务库：load_skill 只读 SOP 正文，search_policy 只读知识库，
# delegate 的执行面由角色自己限定（角色工具面里没有写操作，见 agent_roles）。
AUXILIARY_READ_TOOLS = frozenset(
    {"load_skill", "read_skill_file", "search_policy", "delegate"}
)


def tier_of(tool_name: str) -> str:
    """这个工具有多大权力。业务工具查表，编排层追加的按 ``AUXILIARY_READ_TOOLS`` 判，
    其余**一律按 FUND 处理**。

    fail-closed 的理由：这张表是治理唯一的输入，而"模型能调的东西"会随编排层长大
    （今天加了 skill，明天可能接 CRM）。漏登记时如果默认 READ，那个新工具就直接
    拿到"不过人审就能执行"的待遇；而 READ 的语义是"只查不改"，一个没登记过的工具
    恰恰最可能是在改东西。按 FUND 走的表现是"每次都挂起来等人批"——多一次摩擦，
    不丢安全性，而且漏登记会在工单台上立刻显眼。
    """
    tier = TIER_BY_TOOL.get(tool_name)
    if tier is not None:
        return tier
    return READ if tool_name in AUXILIARY_READ_TOOLS else FUND


def requires_approval(tool_name: str) -> bool:
    return tier_of(tool_name) == FUND


def _humanize(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ())) or "参数"
        parts.append(f"{location}: {error.get('msg')}")
    return "；".join(parts) or "参数不符合该工具的约定"


def _yuan(value: Decimal | None) -> str:
    if value is None:
        return "未知"
    return f"{value.quantize(Decimal('0.01'))} 元"


def _no_order(order_no: str) -> BusinessReject:
    # 明确让模型回头跟客户确认，而不是自己去试一个"看起来相近"的号——
    # 猜中别人的订单比查不到严重得多（下一步退款就退到别人账上了）
    return BusinessReject(
        f"查无此单：{order_no}。这个号可能抄错了，或者不属于本工作区。"
        "不要换一个看起来相近的号再试，回头向客户确认完整订单号。"
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def find_order(db: Session, workspace_id: str, order_no: str) -> CsOrder | None:
    return (
        db.query(CsOrder)
        .filter(CsOrder.workspace_id == workspace_id, CsOrder.order_no == (order_no or "").strip())
        .first()
    )


def _must_find_order(db: Session, workspace_id: str, order_no: str) -> CsOrder:
    """**接缝停在这里。** 只有写路径还用得着它，因为写要改的就是这一行。

    读侧已经全部改走 ``_system(...)`` 拿值对象了（见 business_system 的模块文档串）。
    写侧留一个 ORM 对象不是偷懒：退款必须和它的账本行在**同一个事务**里落，
    而那个事务就是这个 session。抽象成"快照进、结果出"之前，先把这条保证用测试
    钉住（``test_ticket_tools.py`` 里那几条"账本行与业务行同时可见"的断言），
    否则搬动它的时候症状是退款记了账而订单金额没变——不报错，只是账对不上。
    """
    order = find_order(db, workspace_id, order_no)
    if order is None:
        raise _no_order(order_no)
    return order


def _order_lines(order: OrderSnapshot) -> str:
    refundable = order.refundable
    lines = [
        f"订单 {order.order_no}：状态 {order.status}，实付 {_yuan(order.total_amount)}，"
        f"已退 {_yuan(order.refunded_amount)}，可退 {_yuan(refundable)}"
    ]
    for item in order.lines:
        lines.append(
            f"  - {item.title}（{item.sku}）×{item.quantity}，单价 {_yuan(item.unit_price)}"
            + (f"，行状态 {item.line_status}" if item.line_status else "")
        )
    if order.address_text:
        who = " ".join(part for part in (order.receiver_name, order.receiver_phone) if part)
        lines.append(f"  收货：{order.address_text}" + (f"（{who}）" if who else ""))
    return "\n".join(lines)


def _shipment_lines(shipment: ShipmentSnapshot) -> str:
    return (
        f"  {shipment.carrier} {shipment.tracking_no}：{shipment.status}"
        + (f"，最新：{shipment.last_event}" if shipment.last_event else "")
    )


def lookup_order(db: Session, workspace_id: str, *, order_no: str) -> str:
    order = _system(db, workspace_id).get_order(order_no)
    if order is None:
        raise _no_order(order_no)
    text = _order_lines(order)
    if order.shipments:
        text += "\n物流：\n" + "\n".join(_shipment_lines(item) for item in order.shipments)
    return text


def lookup_logistics(db: Session, workspace_id: str, *, order_no: str = "", tracking_no: str = "") -> str:
    system = _system(db, workspace_id)
    if tracking_no.strip():
        # 租户隔离写在实现里（见 business_system 的模块文档串）：调用方拿不到
        # "绕过 join 的裸查询"这个选项。报错也不区分"不存在"和"不是你的"——
        # 那条边界一旦泄露，就变成"这个运单号确实存在，只是在别人手里"。
        shipments = system.find_shipments_by_tracking(tracking_no)
        if not shipments:
            raise BusinessReject(f"查无运单：{tracking_no}。请核对客户提供的运单号是否完整。")
        return "物流：\n" + "\n".join(_shipment_lines(item) for item in shipments)

    order = system.get_order(order_no)
    if order is None:
        raise _no_order(order_no)
    if not order.shipments:
        raise BusinessReject(
            f"订单 {order.order_no} 还没有发货记录（当前状态 {order.status}）。"
            "没有发货不等于丢件，先按状态向客户说明，不要承诺时效。"
        )
    return f"订单 {order.order_no} 的物流：\n" + "\n".join(
        _shipment_lines(item) for item in order.shipments
    )


def lookup_customer(db: Session, workspace_id: str, *, customer_id: str = "", email: str = "", phone: str = "") -> str:
    customer = _system(db, workspace_id).find_customer(
        customer_id=customer_id, email=email, phone=phone
    )
    if customer is None:
        raise BusinessReject(
            "没有找到这个客户。可能是邮箱或号码抄错了——回头向客户确认，"
            "不要凭一个相近的地址替他建档。"
        )
    flags = f"，风险标记：{'、'.join(customer.risk_flags)}" if customer.risk_flags else ""
    return (
        f"客户 {customer.display_name or customer.id}（档位 {customer.tier}）"
        f"累计消费 {_yuan(customer.lifetime_amount)}{flags}；"
        f"在途订单 {len(customer.open_order_nos)} 笔："
        + ("、".join(customer.open_order_nos) or "无")
    )


# ---------------------------------------------------------------------------
# 写操作：幂等 + 治理 + 账本走同一条通道
# ---------------------------------------------------------------------------


def _replay_text(row: CsOperation) -> str:
    return (
        f"重复调用：这笔操作此前已经发起过（账本 {row.id}，状态 {row.status}，"
        f"时间 {row.created_at:%Y-%m-%d %H:%M}）。首次的结果是："
        f"{row.result_excerpt or '（没有留下正文）'}"
        "不要再次执行，把这一条直接答复给客户。"
    )


def execute_write(
    db: Session,
    workspace_id: str,
    *,
    tool_name: str,
    operation: str,
    arguments: dict[str, Any],
    apply: Callable[[], str],
    ticket_id: str | None = None,
    actor: str | None = None,
    amount: Decimal | None = None,
    currency: str | None = None,
) -> str:
    """一次写操作的完整通道：账本在前、治理在后、业务最后，全在一个事务里。

    顺序是**先查账本、再问治理、然后才动业务数据**：查账本挡掉了重复，
    问治理挡掉了暂停与超额，两步都在任何不可逆的动作之前——一笔已经改到一半
    才被拦下的退款，比一笔根本没开始的退款难收拾得多。

    三种出口各有归属：
      - 重复调用 → 返回**首次结果**的文本。这是答复，不是失败。
      - 治理拦下 → 记 ``blocked`` 并返回文本。"额度不够，请转人工"是给模型的
        一条可执行指示，不是这次动作不成立。
      - 业务拒绝（``apply`` 抛出的 ``BusinessReject``）→ 先记 ``failed`` 再原样抛出。
        先记账是因为这一行是"它试过了"的唯一证据；抛出是因为拒绝的语义要穿过
        handler 边界，由那里统一翻成回灌文本——两条通道不该在此处合流成第三种。
    """
    permission = TIER_BY_TOOL[tool_name]
    key = (str(arguments.get("idempotency_key") or "")).strip() or ledger.derive_key(
        ticket_id=ticket_id, tool_name=tool_name, operation=operation, arguments=arguments
    )

    existing = ledger.find(db, workspace_id, key)
    if existing is not None:
        return _replay_text(existing)

    verdict = governor.check_write(db, workspace_id, permission=permission, amount=amount)
    if not verdict.allowed:
        # 拦下来也要记账：没有这一行，"Agent 想退但被限额挡住了"和"Agent 压根
        # 没提这笔退款"在回放里长得一模一样
        ledger.open_row(
            db,
            workspace_id=workspace_id,
            ticket_id=ticket_id,
            tool_name=tool_name,
            operation=operation,
            permission=permission,
            idempotency_key=key,
            arguments=arguments,
            status="blocked",
            amount=amount,
            currency=currency,
            actor=actor,
            blocked_reason=verdict.reason,
        )
        return f"这次操作没有执行：{verdict.reason}。请转人工处理，不要重试这个操作。"

    try:
        result = apply()
    except BusinessReject as exc:
        ledger.open_row(
            db,
            workspace_id=workspace_id,
            ticket_id=ticket_id,
            tool_name=tool_name,
            operation=operation,
            permission=permission,
            idempotency_key=key,
            arguments=arguments,
            status="failed",
            amount=amount,
            currency=currency,
            actor=actor,
            blocked_reason=exc.message,
        )
        raise

    row = ledger.open_row(
        db,
        workspace_id=workspace_id,
        ticket_id=ticket_id,
        tool_name=tool_name,
        operation=operation,
        permission=permission,
        idempotency_key=key,
        arguments=arguments,
        status="executed",
        amount=amount,
        currency=currency,
        actor=actor,
    )
    row.result_excerpt = result[:2000]
    settled = ledger.commit_or_replay(db, row)
    if settled is not row:
        return _replay_text(settled)
    # 走到这里就是一次真的改变了状态的写（失败已经抛出、拦截已经返回），
    # 所以无条件进防篡改链。blocked 与 failed 有各自的账本行——那是"它试过了"
    # 的证据，而这条链回答的是"这串动作有没有被事后改过"，把没发生的动作
    # 也钉进去，链子就退化成一份流水日志。
    #
    # 位置在 commit_or_replay 之后而不是之前：``audit_log.record`` 自己会 commit，
    # 放在前面等于让业务写提前落盘，后面撞键时"账本与业务同行"那条保证就没了。
    audit_log.record(
        db,
        actor_id=actor or "ticket-agent",
        action="ticket.write",
        target=(
            f"{operation}:{arguments.get('order_no') or arguments.get('title') or ''}"
        ).rstrip(":"),
        arguments=arguments,
        ticket_id=ticket_id,
    )
    return result


def update_order_address(
    db: Session,
    workspace_id: str,
    *,
    order_no: str,
    receiver_name: str = "",
    receiver_phone: str = "",
    address_text: str = "",
    idempotency_key: str = "",
    ticket_id: str | None = None,
    actor: str | None = None,
) -> str:
    order = _must_find_order(db, workspace_id, order_no)
    arguments = {
        "order_no": order.order_no,
        "receiver_name": receiver_name,
        "receiver_phone": receiver_phone,
        "address_text": address_text,
        "idempotency_key": idempotency_key,
    }

    def apply() -> str:
        # 状态检查在 apply 里面而不是外面：**幂等先于业务校验**。重试一笔已经改好
        # 的地址应该拿回"这笔做过，结果是……"，而不是"已经发货了改不了"——
        # 后者会让模型以为这次失败是新问题，转而去试一件更糟的事。
        if order.status in _LOCKED_ORDER_STATUSES:
            raise BusinessReject(
                f"订单 {order.order_no} 当前是 {order.status}，收货信息改不了了。"
                "已经发出去的单子要改地址得联系仓库拦截，那是人工的事，不要在这里重试。"
            )
        if receiver_name.strip():
            order.receiver_name = receiver_name.strip()
        if receiver_phone.strip():
            order.receiver_phone = receiver_phone.strip()
        if address_text.strip():
            order.address_text = address_text.strip()
        order.updated_at = naive_now()
        return (
            f"订单 {order.order_no} 的收货信息已更新："
            f"{order.receiver_name or '（未填收件人）'} {order.receiver_phone or ''} "
            f"{order.address_text or '（未填地址）'}".rstrip()
        )

    return execute_write(
        db,
        workspace_id,
        tool_name="update_order_address",
        operation="order.update_address",
        arguments=arguments,
        apply=apply,
        ticket_id=ticket_id,
        actor=actor,
    )


def request_invoice(
    db: Session,
    workspace_id: str,
    *,
    order_no: str,
    title: str,
    tax_no: str = "",
    email: str = "",
    idempotency_key: str = "",
    ticket_id: str | None = None,
    actor: str | None = None,
) -> str:
    order = _must_find_order(db, workspace_id, order_no)
    arguments = {
        "order_no": order.order_no,
        "title": title,
        "tax_no": tax_no,
        "email": email,
        "idempotency_key": idempotency_key,
    }

    def apply() -> str:
        if order.status == "cancelled":
            raise BusinessReject(f"订单 {order.order_no} 已取消，不能再开票。")
        pending = (
            db.query(CsInvoice)
            .filter(
                CsInvoice.workspace_id == workspace_id,
                CsInvoice.order_id == order.id,
                CsInvoice.title == title.strip(),
                CsInvoice.status.in_(("requested", "issued")),
            )
            .first()
        )
        if pending is not None:
            # 这一步在事务里、却想中止整笔操作：execute_write 会先把它记成 failed
            # 再原样抛出，业务数据一行都没改
            raise BusinessReject(
                f"这张发票此前已经申请过（{pending.status}，抬头 {pending.title}），"
                "不要重复提交。"
            )
        record = CsInvoice(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            order_id=order.id,
            customer_id=order.customer_id,
            ticket_id=ticket_id,
            title=title.strip()[:200],
            tax_no=(tax_no or "").strip()[:40] or None,
            email=(email or "").strip().lower() or None,
            status="requested",
            idempotency_key=(idempotency_key or "").strip() or None,
            created_at=naive_now(),
            updated_at=naive_now(),
        )
        db.add(record)
        return f"发票申请已提交：抬头 {record.title}，订单 {order.order_no}，等待开票。"

    return execute_write(
        db,
        workspace_id,
        tool_name="request_invoice",
        operation="invoice.request",
        arguments=arguments,
        apply=apply,
        ticket_id=ticket_id,
        actor=actor,
    )


def create_refund(
    db: Session,
    workspace_id: str,
    *,
    order_no: str,
    amount: str,
    reason_code: str = "",
    idempotency_key: str = "",
    approved_by: str | None = None,
    ticket_id: str | None = None,
    actor: str | None = None,
) -> str:
    order = _must_find_order(db, workspace_id, order_no)
    requested = parse_amount(amount)
    # 金额解析留在外面：那是**参数**不成立，不是一次业务动作，既不该记账，
    # 也不该被当成一笔金额未知的退款去触发额度最坏情况的判定
    if requested is None or requested <= 0:
        raise BusinessReject(
            f"退款金额不成立：{amount!r}。请从原文里逐字取一个正数，"
            "不要用一个猜测的数字重试。"
        )
    arguments = {
        "order_no": order.order_no,
        "amount": str(requested),
        "reason_code": reason_code,
        "idempotency_key": idempotency_key,
    }
    resolved_key = (idempotency_key or "").strip() or ledger.derive_key(
        ticket_id=ticket_id, tool_name="create_refund", operation="refund.create", arguments=arguments
    )

    def apply() -> str:
        # 同改地址那条：**幂等先于业务校验**。一笔已经退好的款，重试要拿回
        # "这笔做过"，而不是"已经没有可退的余额了"
        if order.status in ("cancelled", "refunded"):
            raise BusinessReject(
                f"订单 {order.order_no} 当前是 {order.status}，已经没有可退的余额了。"
            )
        refundable = order.total_amount - order.refunded_amount
        if requested > refundable:
            raise BusinessReject(
                f"退款金额超出可退余额：申请 {_yuan(requested)}，"
                f"而这单最多可退 {_yuan(refundable)}（实付 {_yuan(order.total_amount)}，"
                f"已退 {_yuan(order.refunded_amount)}）。按可退余额重新发起，"
                "或者向客户说明差额部分为什么退不了。"
            )
        now = naive_now()
        record = CsRefund(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            order_id=order.id,
            customer_id=order.customer_id,
            ticket_id=ticket_id,
            amount=requested,
            currency=order.currency,
            # 走到这里说明人已经点过头（编排层在挂起处拦资金类工具），
            # 所以是直接 executed 而不是 requested。真实部署里这一态由支付网关回调写。
            status="executed",
            reason_code=(reason_code or "").strip()[:40] or None,
            idempotency_key=resolved_key,
            requested_by=actor,
            approved_by=approved_by,
            approved_at=now if approved_by else None,
            executed_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(record)
        order.refunded_amount = order.refunded_amount + requested
        if order.refunded_amount >= order.total_amount:
            order.status = "refunded"
        order.updated_at = now
        return (
            f"退款已发起：{_yuan(requested)}，订单 {order.order_no}，"
            f"剩余可退 {_yuan(order.total_amount - order.refunded_amount)}。"
        )

    return execute_write(
        db,
        workspace_id,
        tool_name="create_refund",
        operation="refund.create",
        arguments=arguments,
        apply=apply,
        ticket_id=ticket_id,
        actor=actor,
        amount=requested,
        currency=order.currency,
    )


def cancel_order(
    db: Session,
    workspace_id: str,
    *,
    order_no: str,
    reason: str = "",
    idempotency_key: str = "",
    ticket_id: str | None = None,
    actor: str | None = None,
) -> str:
    order = _must_find_order(db, workspace_id, order_no)
    arguments = {
        "order_no": order.order_no,
        "reason": reason,
        "idempotency_key": idempotency_key,
    }

    def apply() -> str:
        if order.status in _LOCKED_ORDER_STATUSES:
            raise BusinessReject(
                f"订单 {order.order_no} 当前是 {order.status}，取消不掉了。"
                "已发货的包裹要走退货流程，那是另一件事，别在这里重试。"
            )
        order.status = "cancelled"
        order.cancelled_at = naive_now()
        order.updated_at = order.cancelled_at
        # 取消**不等于退款**：那是另一个资金类操作，要单独过审批。
        # 把它捆在这儿等于让"取消"这个中风险动作悄悄退了一笔钱。
        return (
            f"订单 {order.order_no} 已取消。"
            + (f"原因：{reason.strip()[:80]}。" if reason.strip() else "")
            + "注意：款项还没有退，需要退款请另外发起。"
        )

    return execute_write(
        db,
        workspace_id,
        tool_name="cancel_order",
        operation="order.cancel",
        arguments=arguments,
        apply=apply,
        ticket_id=ticket_id,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# 工具面组装
# ---------------------------------------------------------------------------

_DESCRIPTIONS = {
    "lookup_order": "按订单号查订单：状态、金额、可退余额、商品行、收货信息。",
    "lookup_logistics": "查物流：按订单号或运单号，返回承运商、状态、最新一条轨迹。",
    "lookup_customer": "查客户档案：档位、累计消费、风险标记、在途订单。",
    "update_order_address": "修改订单的收货人 / 电话 / 地址。已发货之后改不了。",
    "request_invoice": "为客户提交发票申请（抬头、税号、收件邮箱）。",
    "create_refund": "发起退款。资金类操作，必须已经拿到人工批准。",
    "cancel_order": "取消整张还没发货的订单。取消不等于退款。",
}


def build(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    ticket_id: str | None = None,
    max_risk: str = "low",
) -> list[ToolDefinition]:
    """按风险档位装配工具面——文档§4 的"工具调用白名单"就是这里。

    ``max_risk`` 决定开到哪一档：low 只给查询，mid 加修改类，high 加资金类。
    **默认是 low**，所以"忘了传风险等级"的后果是 Agent 只能查、不能改，
    而不是拿着退款工具去办一张查物流的工单。

    资金类工具出现在工具面里不等于它能自己跑：审批闸门在编排层（见
    ``requires_approval``），这里只决定模型看不看得见。
    """
    floor = _RISK_FLOOR.get(max_risk, 0)

    handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
        "lookup_order": lambda args: lookup_order(
            db, workspace_id, order_no=args["order_no"]
        ),
        "lookup_logistics": lambda args: lookup_logistics(
            db, workspace_id, order_no=args.get("order_no", ""), tracking_no=args.get("tracking_no", "")
        ),
        "lookup_customer": lambda args: lookup_customer(
            db,
            workspace_id,
            customer_id=args.get("customer_id", ""),
            email=args.get("email", ""),
            phone=args.get("phone", ""),
        ),
        "update_order_address": lambda args: update_order_address(
            db,
            workspace_id,
            order_no=args["order_no"],
            receiver_name=args.get("receiver_name", ""),
            receiver_phone=args.get("receiver_phone", ""),
            address_text=args.get("address_text", ""),
            idempotency_key=args.get("idempotency_key", ""),
            ticket_id=ticket_id,
            actor=user_id,
        ),
        "request_invoice": lambda args: request_invoice(
            db,
            workspace_id,
            order_no=args["order_no"],
            title=args["title"],
            tax_no=args.get("tax_no", ""),
            email=args.get("email", ""),
            idempotency_key=args.get("idempotency_key", ""),
            ticket_id=ticket_id,
            actor=user_id,
        ),
        "create_refund": lambda args: create_refund(
            db,
            workspace_id,
            order_no=args["order_no"],
            amount=args["amount"],
            reason_code=args.get("reason_code", ""),
            idempotency_key=args.get("idempotency_key", ""),
            approved_by=user_id,
            ticket_id=ticket_id,
            actor=user_id,
        ),
        "cancel_order": lambda args: cancel_order(
            db,
            workspace_id,
            order_no=args["order_no"],
            reason=args.get("reason", ""),
            idempotency_key=args.get("idempotency_key", ""),
            ticket_id=ticket_id,
            actor=user_id,
        ),
    }

    models: dict[str, type[BaseModel]] = {
        "lookup_order": LookupOrderArgs,
        "lookup_logistics": LookupLogisticsArgs,
        "lookup_customer": LookupCustomerArgs,
        "update_order_address": UpdateAddressArgs,
        "request_invoice": RequestInvoiceArgs,
        "create_refund": CreateRefundArgs,
        "cancel_order": CancelOrderArgs,
    }

    definitions: list[ToolDefinition] = []
    for name, tier in TIER_BY_TOOL.items():
        if _TIER_FLOOR[tier] > floor:
            continue
        model = models[name]
        call = handlers[name]

        async def handler(arguments: dict[str, Any], *, _call=call, _model=model) -> str:
            try:
                args = _model.model_validate(arguments).model_dump()
            except ValidationError as exc:
                # 到这里的都是 schema 那层查不出的业务约束（"至少给一个"、金额格式）。
                # 回灌的必须是一句能照着改的中文，不是 Pydantic 的英文堆栈。
                return f"参数不成立：{_humanize(exc)}"
            return await _run_sync(_call, args)

        definitions.append(
            ToolDefinition(
                name=name,
                description=_DESCRIPTIONS[name],
                parameters=model.model_json_schema(),
                handler=handler,
            )
        )
    return definitions


async def _run_sync(call: Callable[[dict[str, Any]], Any], args: dict[str, Any]) -> str:
    """业务函数是同步的（它们只是 ORM），工具契约要的是协程。

    ``BusinessReject`` 之外的异常**不在这里接**：那是真故障，让运行时的熔断器去
    数它，而不是让模型以为"这个工具每次都这么难用"。
    """
    try:
        return call(args)
    except BusinessReject as exc:
        return exc.message
