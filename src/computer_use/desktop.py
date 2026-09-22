"""平台边界：核心逻辑与 Windows 之间唯一的缝。

核心不 import 任何 Win32/UIA/截图库，只依赖这里的 `DesktopPort`。
真实实现见 `win32_desktop.py`，测试替身见 `tests/fake_desktop.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class Rect:
    """屏幕坐标系下的矩形，单位为物理像素。"""

    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class Window:
    """桌面上一个顶层窗口。

    `handle` 是 Win32 的 HWND，后续的按窗口截图与命中测试都以它指称窗口。
    """

    handle: int
    title: str
    process_name: str
    rect: Rect
    is_visible: bool
    is_minimized: bool


class DesktopPort(Protocol):
    """桌面能提供的原始事实。这里只做采集，不做判定。"""

    def list_windows(self) -> Sequence[Window]:
        """枚举所有顶层窗口，含不可见与最小化的，交由核心筛选。"""
        ...
