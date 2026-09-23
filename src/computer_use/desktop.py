"""平台边界：核心逻辑与 Windows 之间唯一的缝。

核心不 import 任何 Win32/UIA/截图库，只依赖这里的 `DesktopPort`。
真实实现见 `win32_desktop.py`，测试替身见 `tests/fake_desktop.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from PIL.Image import Image


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


@dataclass(frozen=True)
class Capture:
    """一个窗口在某一刻的样子。

    `image` 的每个像素对应屏幕上的一个物理像素，尺寸与 `rect` 相同；
    `rect` 是图像所画区域在屏幕上的位置，可能与窗口矩形略有出入（例如去掉了不可见的缩放边框）。
    `dpi_scale` 是窗口所在显示器的缩放比，125% 即 1.25。
    """

    image: Image
    rect: Rect
    dpi_scale: float


class WindowUnavailable(Exception):
    """指称的窗口不存在，或已经无法采集。"""


class DesktopPort(Protocol):
    """桌面能提供的原始事实。这里只做采集，不做判定。"""

    def list_windows(self) -> Sequence[Window]:
        """枚举所有顶层窗口，含不可见与最小化的，交由核心筛选。"""
        ...

    def capture_window(self, handle: int) -> Capture:
        """截取一个窗口本身，被其他窗口遮住的部分也照常画出。

        窗口已经不在时抛 `WindowUnavailable`。
        """
        ...

    def append_log(self, line: str) -> None:
        """把一行记录追加到动作日志末尾。`line` 不含换行。"""
        ...

    def save_evidence(self, png: bytes) -> str:
        """保存一张留证截图，返回它的位置，供日志引用。"""
        ...
