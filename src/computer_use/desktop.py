"""平台边界：核心逻辑与 Windows 之间唯一的缝。

核心不 import 任何 Win32/UIA/截图库，只依赖这里的 `DesktopPort`。
真实实现见 `win32_desktop.py`，测试替身见 `tests/fake_desktop.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Collection, Protocol, Sequence

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
    `process_name` 是所属进程的可执行文件名，够不到那个进程（例如它是提权进程）时为空串。
    `owner` 是它的所有者窗口（对话框、弹出菜单所依附的那个窗口），没有时为 `None`。
    """

    handle: int
    title: str
    process_id: int
    process_name: str
    rect: Rect
    is_visible: bool
    is_minimized: bool
    owner: int | None


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


class TextUnreadable(Exception):
    """文字识别无法进行，例如系统里没有可用的识别语言。"""


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

    def window_at(self, x: int, y: int) -> int | None:
        """屏幕物理像素 `(x, y)` 处最上层的顶层窗口；那里没有窗口时为 `None`。"""
        ...

    def agent_process_ids(self) -> Collection[int]:
        """Agent 自身所在的进程：本服务及其各级父进程，其中之一持有 Agent 所在的窗口。"""
        ...

    def recognize_text(self, capture: Capture, region: Rect) -> str:
        """识别 `capture` 中落在屏幕区域 `region` 内的文字，不分行、不保证词序。

        `region` 在 `capture.rect` 之内。识别无法进行时抛 `TextUnreadable`。
        """
        ...

    def click(self, x: int, y: int) -> None:
        """在屏幕物理像素 `(x, y)` 处单击鼠标左键。"""
        ...

    def append_log(self, line: str) -> None:
        """把一行记录追加到动作日志末尾。`line` 不含换行。"""
        ...

    def save_evidence(self, png: bytes) -> str:
        """保存一张留证截图，返回它的位置，供日志引用。"""
        ...

    def put_ticket(self, key: str, issued_at: datetime) -> None:
        """存下一张裁决凭据。服务端与 hook 是两个进程，凭据须存在两者都够得到的地方。"""
        ...

    def take_ticket(self, key: str) -> datetime | None:
        """取走一张裁决凭据，返回它的签发时刻；没有时为 `None`。同一张凭据只能被取走一次。"""
        ...
