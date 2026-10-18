"""UTF-8 输出开关。

钉的是一件很具体的事：**门禁要在需要报不合格的那一刻活得下来**。渲染 verdict 会打
`✗`，启动横幅的警告分支会打 `⚠`，这两个字符都不在 GBK 里；Windows 上输出被重定向
时 Python 按 locale 编码，于是在最关键的一行抛 UnicodeEncodeError。
"""
import io

import pytest

from services.console import utf8_output


def test_非gbk字符打得出来了(monkeypatch):
    """切换之前那句会炸，切换之后不炸——这条只有在"真的换了编码"时才有意义。"""
    buffer = io.BytesIO()

    class Pipe:
        """模拟重定向出去的 stdout：按当前编码写字节，写不进就抛。"""

        def __init__(self) -> None:
            self.encoding = "gbk"
            self.errors = "strict"
            self.buffer = buffer

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            self.encoding = encoding
            self.errors = errors

        def write(self, text: str) -> int:
            data = text.encode(self.encoding, self.errors)
            self.buffer.write(data)
            return len(data)

    pipe = Pipe()
    line = "变体 ticket：不达标 ✗ ⚠ 运行不可信"
    # 先自证这个 fixture 是有牙的：GBK 严格编码装不下这两个符号
    with pytest.raises(UnicodeEncodeError):
        line.encode("gbk", "strict")

    monkeypatch.setattr("sys.stdout", pipe)
    utf8_output()
    pipe.write(line)

    assert "不达标".encode("utf-8") in buffer.getvalue()


def test_流没有reconfigure时安静放过(monkeypatch):
    class Bare:
        def write(self, text: str) -> int:
            return len(text)

    monkeypatch.setattr("sys.stdout", Bare())
    monkeypatch.setattr("sys.stderr", object())  # 连 write 都没有
    utf8_output()  # 不该抛


def test_reconfigure抛错时不影响调用方(monkeypatch):
    class Refuses:
        encoding = "gbk"

        def reconfigure(self, **kwargs):
            raise ValueError("已经绑定了别的缓冲")

        def write(self, text: str) -> int:
            return len(text)

    monkeypatch.setattr("sys.stdout", Refuses())
    utf8_output()
