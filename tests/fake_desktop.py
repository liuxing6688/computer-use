"""`DesktopPort` 的测试替身：持有一份可脚本化的窗口布局。"""

from __future__ import annotations

from typing import Sequence

from computer_use.desktop import Rect, Window


class FakeDesktop:
    """按给定的窗口布局回答枚举，不需要真实桌面。"""

    def __init__(self, windows: Sequence[Window] = ()) -> None:
        self._windows = list(windows)

    def list_windows(self) -> Sequence[Window]:
        return tuple(self._windows)


def window(
    *,
    handle: int = 1,
    title: str = "无标题 - 记事本",
    process_name: str = "notepad.exe",
    rect: Rect = Rect(left=0, top=0, width=800, height=600),
    is_visible: bool = True,
    is_minimized: bool = False,
) -> Window:
    """构造一个窗口，默认是一个可见、有标题的普通窗口。"""

    return Window(
        handle=handle,
        title=title,
        process_name=process_name,
        rect=rect,
        is_visible=is_visible,
        is_minimized=is_minimized,
    )
