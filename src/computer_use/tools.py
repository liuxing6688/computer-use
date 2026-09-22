"""工具层：把核心的观察结果整理成可回传给模型的形状。

这里不做判定，判定在 `windows.py`。
"""

from __future__ import annotations

from typing import Any

from computer_use.desktop import DesktopPort, Window
from computer_use.windows import operable_windows


def list_windows(desktop: DesktopPort) -> list[dict[str, Any]]:
    """只读工具：列出桌面上可操作的窗口。"""

    return [_as_payload(w) for w in operable_windows(desktop)]


def _as_payload(window: Window) -> dict[str, Any]:
    return {
        "handle": window.handle,
        "title": window.title,
        "process_name": window.process_name,
        "rect": {
            "left": window.rect.left,
            "top": window.rect.top,
            "width": window.rect.width,
            "height": window.rect.height,
        },
    }
