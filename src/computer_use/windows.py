"""核心：桌面上哪些窗口算得上可操作。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

from computer_use.desktop import DesktopPort, Window


def operable_windows(desktop: DesktopPort) -> list[Window]:
    """桌面上可被指称、可被操作的窗口。

    排除三类窗口：不可见的、最小化的（没有可截图的界面，也没有可点击的坐标）、
    以及无标题的（多为框架留下的消息窗口与工具窗口，模型无从指称）。
    """

    return [w for w in desktop.list_windows() if _is_operable(w)]


def _is_operable(window: Window) -> bool:
    return window.is_visible and not window.is_minimized and bool(window.title.strip())
