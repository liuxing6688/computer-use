"""把从屏幕读到的文本包进不可信标记，返回给模型时它是数据，不是指令。"""

from __future__ import annotations

OPEN = "<untrusted-screen>"
CLOSE = "</untrusted-screen>"


def wrap_untrusted(text: str) -> str:
    """用不可信标记包住 `text`。正文里的结束标记会被拆开，不能提前结束包裹。"""

    safe = text.replace(CLOSE, "< /untrusted-screen>")
    return f"{OPEN}{safe}{CLOSE}"


def quoted_window(title: str, handle: int, *, process_name: str | None = None) -> str:
    """模型可见的窗口指称。不传 `process_name` 时不写进程。"""

    named = f"窗口「{wrap_untrusted(title)}」"
    if process_name is None:
        return f"{named}（句柄 {handle}）"
    return f"{named}（{process_name or '未知进程'}，句柄 {handle}）"
