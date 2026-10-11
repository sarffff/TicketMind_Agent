"""发送通道：把"这条话该交给谁"这件事收在一个地方。

原来这个判断长在 ``routers/ticket_router.py`` 的 ``_make_sender()`` 里。挪出来是因为
自动排空循环也要用它，而 ``services`` 反过来 import ``routers`` 是层序倒置——
router 在最上面。

**一种通道都没配时 ``build_sender()`` 返回 None**，那是默认状态，不是待补的 TODO。
留白处的行为必须是**诚实失败**而不是假装成功，理由见 ``outbox.deliver`` 的文档串：
把 pending 悄悄刷成 sent 会让 ``ticket_outbox`` 这张表从此不能用来回答
"客户收到回话了吗"，而它正是为这个问题存在的。

## 一个通道只声明它真正覆盖的工单渠道

SMTP 只覆盖 ``email``。``web_chat`` / ``app`` / ``wecom`` 的行不会被它认领出去
（``outbox.claim_next`` 按 ``channels`` 过滤），它们会继续躺在 pending 里被看见。
用邮件通道把网页聊天的回复标成"已发送"，是比不发更坏的结果——那是一次不存在的送达。

## 半套配置等于没有配置

``SMTP_HOST`` 给了但 ``SMTP_FROM`` 空着、``SMTP_SECURITY`` 写了个不认的值——这些都
返回 None 并说明缺了什么，而不是挑一个默认值试一次。否则第一次排空会连着失败三条，
而报错说的是服务商返回了什么，没人会想到真正的原因是没填发件地址。
"""
from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from typing import FrozenSet

from config import settings
from services.ticket.outbox import Sender

logger = logging.getLogger("ticket.senders")

# 这一条通道能真正送达的工单渠道。只有 email：邮件地址是收件人，别的渠道的
# "收件人"字段对 SMTP 没有任何意义。
SMTP_CHANNELS: FrozenSet[str] = frozenset({"email"})

_SECURITY_STARTTLS = "starttls"
_SECURITY_SSL = "ssl"
_SECURITY_NONE = "none"
_SECURITY_VALUES = (_SECURITY_STARTTLS, _SECURITY_SSL, _SECURITY_NONE)


@dataclass(frozen=True, slots=True)
class OutboundChannel:
    """一个能用的出口。``channels`` 声明它覆盖哪些工单渠道，``send`` 抛异常算失败。"""

    label: str
    channels: FrozenSet[str]
    send: Sender


def build_sender() -> OutboundChannel | None:
    """按配置返回当前可用的出口通道；一种都没有时返回 None。"""
    smtp = _smtp_channel()
    if smtp is not None:
        return smtp
    return None


def describe() -> str:
    """给启动横幅用的一行话。宁可长一点，也不要让人猜到底通不通。"""
    channel = build_sender()
    if channel is None:
        return "none（没有接入任何发送通道，待发队列只增不减）"
    return f"{channel.label} → {'、'.join(sorted(channel.channels))}"


def _smtp_channel() -> OutboundChannel | None:
    host = (settings.SMTP_HOST or "").strip()
    if not host:
        return None

    missing: list[str] = []
    if not (settings.SMTP_FROM or "").strip():
        missing.append("SMTP_FROM（发件地址）")
    security = (settings.SMTP_SECURITY or "").strip().lower()
    if security not in _SECURITY_VALUES:
        missing.append(
            f"SMTP_SECURITY（不认识的值 {security!r}，可选 {'/'.join(_SECURITY_VALUES)}）"
        )
    if missing:
        logger.warning(
            "SMTP_HOST 已配置但通道不完整（%s）——按「没有出口通道」处理，"
            "待发队列不会被发送",
            "、".join(missing),
        )
        return None

    if security == _SECURITY_NONE and (settings.SMTP_USERNAME or "").strip():
        # 不拦：本机 relay 确实存在。但明文发凭据这件事必须留一条日志，
        # 否则它会在某天变成一封"我们从来没这么配过"的审计报告。
        logger.warning("SMTP_SECURITY=none：凭据与正文将以明文发出")

    port = int(settings.SMTP_PORT or 0) or (465 if security == _SECURITY_SSL else 587)
    return OutboundChannel(
        label=f"smtp://{host}:{port}/{security}",
        channels=SMTP_CHANNELS,
        send=_make_smtp_send(),
    )


def _make_smtp_send() -> Sender:
    def send(channel: str, recipient: str | None, body: str) -> None:
        import smtplib
        from email.message import EmailMessage

        if channel not in SMTP_CHANNELS:
            # 认领那一步已经按 channels 过滤过，走到这里说明调用方绕过了它。
            # 静默返回就是一次不存在的送达，所以必须炸出来。
            raise RuntimeError(f"SMTP 通道被喂了一个它不覆盖的渠道：{channel!r}")
        address = (recipient or "").strip()
        if not address:
            raise ValueError("这封邮件没有收件地址（工单只有订单号，没留联系方式）")
        if "\r" in address or "\n" in address:
            # 收件地址是从工单正文里抽出来的，属于外部输入。留在头里就是
            # 一次邮件头注入：多写几行可以改主题、加抄送，甚至换收件人。
            raise ValueError("收件地址含换行，已拒绝")

        message = EmailMessage()
        message["From"] = settings.SMTP_FROM.strip()
        message["To"] = address
        message["Subject"] = f"{settings.SMTP_SUBJECT_PREFIX} 您的工单有进展"
        message.set_content(body)

        host = settings.SMTP_HOST.strip()
        security = (settings.SMTP_SECURITY or "").strip().lower()
        timeout = max(1.0, float(settings.SMTP_TIMEOUT_SECONDS))
        port = int(settings.SMTP_PORT or 0) or (465 if security == _SECURITY_SSL else 587)

        if security == _SECURITY_SSL:
            client = smtplib.SMTP_SSL(
                host, port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        try:
            if security == _SECURITY_STARTTLS:
                # 默认验证证书。要连自签名的内部 relay，请去把 CA 装进信任链，
                # 而不是在这里关掉校验——那等于把"有没有中间人"这个问题删掉。
                client.starttls(context=ssl.create_default_context())
            username = (settings.SMTP_USERNAME or "").strip()
            if username:
                client.login(username, settings.SMTP_PASSWORD or "")
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001 - 关闭失败不该盖掉真正的发送结果
                pass

    return send
