"""外发通道：半套配置等于没有配置，以及一条通道只声明它真能送达的渠道。

这个文件里最要紧的是两条**否决**性质的断言：

1. ``SMTP_HOST`` 给了但 ``SMTP_FROM`` 空着 —— 返回 None，不挑个默认值试一次。
   半套配置如果算"已接通"，第一次排空会连着失败好几条，而报错说的是服务商返回了
   什么，没人会想到真正的原因是没填发件地址。
2. 邮件通道不许把 ``web_chat`` 的回复标成已发送。认领按渠道过滤，所以那种行永远
   躺在 pending 里被看见——一次不存在的送达比不发更坏，因为它进了台账。

收件地址那两条也在这一类里：它是从工单正文抽出来的外部输入，含换行就是邮件头注入。
"""
import smtplib

import pytest

from config import settings
from models import Ticket, TicketOutbox
from services.clock import naive_now
from services.ticket import outbox, senders


@pytest.fixture(autouse=True)
def _no_smtp(monkeypatch):
    """每个用例都从"没配邮件通道"出发。

    不这样做的话，跑测试的人本机 ``.env`` 里一份真实 SMTP 配置会让这些断言
    在某个人的机器上成立、在 CI 上不成立——而这类测试失败的样子是"通道居然是通的"。
    """
    for key in ("SMTP_HOST", "SMTP_FROM", "SMTP_USERNAME", "SMTP_PASSWORD"):
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(settings, "SMTP_SECURITY", "starttls")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)


def _configured(monkeypatch, **overrides):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_FROM", "ticket@example.com")
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


# ========== 什么算"接通了" ==========


def test_没配任何通道时如实说没有():
    assert senders.build_sender() is None
    assert "没有接入任何发送通道" in senders.describe()


def test_只给host没给发件地址算没配(monkeypatch):
    """半套配置不能算"已接通"。见模块文档串第 1 条。"""
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_FROM", "")

    assert senders.build_sender() is None


def test_不认识的security值算没配(monkeypatch):
    """拼错一个字母就退回明文发，是那种没人会发现的降级。"""
    _configured(monkeypatch, SMTP_SECURITY="startls")

    assert senders.build_sender() is None


def test_配齐了才返回通道且只覆盖email(monkeypatch):
    _configured(monkeypatch)
    channel = senders.build_sender()

    assert channel is not None
    assert channel.channels == frozenset({"email"})
    assert "smtp.example.com" in channel.label
    assert "email" in senders.describe()


# ========== 收件地址是外部输入 ==========


@pytest.mark.parametrize(
    "channel_name, recipient, expected",
    [
        ("web_chat", "someone@example.com", "不覆盖的渠道"),
        ("email", None, "没有收件地址"),
        ("email", "  ", "没有收件地址"),
        ("email", "a@b.com\r\nCc: attacker@evil.com", "换行注入"),
    ],
)
def test_该拒的收件人都拒掉(monkeypatch, channel_name, recipient, expected):
    _configured(monkeypatch)
    send = senders.build_sender().send

    with pytest.raises((ValueError, RuntimeError)):
        send(channel_name, recipient, "您的包裹明天到")


def test_渠道不对时抛错而不是静默返回(monkeypatch):
    """静默返回 == 一次不存在的送达。认领已经过滤过，走到这里说明有人绕过了它。"""
    _configured(monkeypatch)
    send = senders.build_sender().send

    with pytest.raises(RuntimeError):
        send("app", "push-token-123", "您的包裹明天到")


# ========== 真的发出去的那一段 ==========


class _FakeSMTP:
    """记下调用，不碰网络。"""

    instances: list = []

    def __init__(self, host, port, timeout=None, **kwargs):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.kwargs = kwargs
        _FakeSMTP.instances.append(self)

    def starttls(self, *, context=None):
        self.calls.append("starttls")
        self.tls_context = context

    def login(self, user, password):
        self.calls.append(f"login:{user}")

    def send_message(self, message):
        self.calls.append("send_message")
        self.message = message

    def quit(self):
        self.calls.append("quit")


def test_默认走starttls且不关证书校验(monkeypatch):
    """``create_default_context()`` 被换成 ``CERT_NONE`` 是这种测试唯一真正防的东西。

    那行改动在功能上"能用"——自签名证书的 relay 照样连上——代价是任何人站在中间
    都看不出来，而客户的工单邮件正文里有姓名、地址、订单号。
    """
    _configured(monkeypatch, SMTP_USERNAME="bot", SMTP_PASSWORD="secret")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    senders.build_sender().send("email", "buyer@example.com", "您的包裹明天到")

    client = _FakeSMTP.instances[-1]
    assert client.calls == ["starttls", "login:bot", "send_message", "quit"]
    assert client.tls_context is not None, "starttls 必须带着一个显式的 SSL context"
    assert client.tls_context.verify_mode, "证书校验不能被关掉"


def test_邮件头带上了发件人与收件人(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    senders.build_sender().send("email", "buyer@example.com", "您的包裹明天到")

    message = _FakeSMTP.instances[-1].message
    assert message["To"] == "buyer@example.com"
    assert message["From"] == "ticket@example.com"
    assert message.get_content().strip() == "您的包裹明天到"
    assert settings.SMTP_SUBJECT_PREFIX in message["Subject"]


def test_ssl端口不请starttls(monkeypatch):
    """465 是隐式 TLS。在已经加密的连线上再 STARTTLS 是一次协议错误。"""
    _configured(monkeypatch, SMTP_SECURITY="ssl", SMTP_PORT=465)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)

    senders.build_sender().send("email", "buyer@example.com", "正文")

    client = _FakeSMTP.instances[-1]
    assert "starttls" not in client.calls
    assert client.port == 465


# ========== 认领只领这个通道能送达的渠道 ==========


def _ticket(db, *, ticket_id, channel):
    now = naive_now()
    row = Ticket(
        id=ticket_id, workspace_id="w1", channel=channel, status="resolved",
        request_text="ORD-1 帮我查查", created_at=now, updated_at=now,
    )
    db.add(row)
    db.commit()
    return row


def test_邮件通道不会把网页聊天的回复领走(db_real, monkeypatch):
    """见模块文档串第 2 条：宁可让它继续躺在 pending 里被看见。"""
    _configured(monkeypatch)
    channel = senders.build_sender()

    _ticket(db_real, ticket_id="t-web", channel="web_chat")
    outbox.enqueue(db_real, db_real.get(Ticket, "t-web"), kind=outbox.KIND_REPLY, body="网页上的回话")
    _ticket(db_real, ticket_id="t-mail", channel="email")
    outbox.enqueue(
        db_real, db_real.get(Ticket, "t-mail"), kind=outbox.KIND_REPLY,
        body="邮件的回话", recipient="buyer@example.com",
    )

    claimed = outbox.claim_next(db_real, "worker-1", channel.channels)

    assert claimed is not None and claimed.channel == "email"
    web = db_real.query(TicketOutbox).filter_by(channel="web_chat").one()
    assert web.status == "pending", "不属于这个渠道的行被动过了"


def test_不传渠道时照旧什么都领(db_real):
    """手动排空那一条路径的语义："有什么发什么"。不能因为加了过滤就把它收窄。"""
    _ticket(db_real, ticket_id="t-web", channel="web_chat")
    outbox.enqueue(db_real, db_real.get(Ticket, "t-web"), kind=outbox.KIND_REPLY, body="网页回话")

    assert outbox.claim_next(db_real, "worker-2") is not None
