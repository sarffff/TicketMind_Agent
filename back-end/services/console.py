"""把标准输出切成 UTF-8。

**为什么需要**：Windows 上 Python 的 stdout 在重定向到管道/文件时用 locale 编码
（这台机器是 cp936/GBK）。而本仓库的输出里不只有中文——门禁渲染 verdict 时会打
`✗`、启动横幅会打 `⚠`，这两个字符都不在 GBK 里。于是症状是：

- `python -m eval.gate` 在**需要报不合格**的那一刻崩在 UnicodeEncodeError 上，
  门禁的结论反而看不见；
- 后端启动时任何一个警告分支都会让进程起不来。

`errors="replace"` 而不是 `strict`：控制台上的一个替换字符远不如"结论没打出来"贵。
只在入口点调用，不放进被 import 的模块——库模块不该在导入时改全局状态。
"""
from __future__ import annotations

import sys


def utf8_output() -> None:
    """尽力把 stdout/stderr 切成 UTF-8。失败就安静放过。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # 被替身对象接管（测试里的 capsys 等）
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # 已经绑定到别的缓冲了。不因此让一次评估或一次启动整体失败
            pass
