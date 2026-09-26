"""平台边界：核心逻辑与 Windows 之间唯一的缝。

核心不 import 任何 Win32/UIA/截图库，只依赖这里的 `DesktopPort`。
真实实现见 `win32_desktop.py`，测试替身见 `tests/fake_desktop.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Collection, Literal, Protocol, Sequence

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


@dataclass(frozen=True)
class RegionChange:
    """落点周围目标区域的像素比较结果。

    `changed` 为真表示这块区域发生了实质变化。
    `changed_pixels` 与 `total_pixels` 是变化了的像素数和区域像素总数，供拦截理由引用。
    核心只使用这个结果，不自己逐像素比较。
    """

    changed: bool
    changed_pixels: int
    total_pixels: int


class WindowUnavailable(Exception):
    """指称的窗口不存在，或已经无法采集。"""


class TextUnreadable(Exception):
    """文字识别无法进行，例如系统里没有可用的识别语言。"""


class ForegroundError(Exception):
    """目标窗口没能来到前台。"""


class ClipboardUnavailable(Exception):
    """剪贴板读、写、粘贴或恢复没能完成。"""


class InjectionError(Exception):
    """逐字符注入没能把字符送进前台窗口。"""


class LaunchError(Exception):
    """进程没能启动。"""


class FileError(Exception):
    """文件操作没能完成；消息原样回给模型。"""


class CommandError(Exception):
    """PowerShell 没能启动，或没能在时限内结束。"""


@dataclass(frozen=True)
class CommandResult:
    """一条 PowerShell 命令的输出。非零退出码也是结果，不是异常。"""

    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True)
class DirEntry:
    """目录里的一个名字。`is_dir` 为真表示它自己也是目录。"""

    name: str
    is_dir: bool


@dataclass(frozen=True)
class Clipboard:
    """一份剪贴板内容。核心不解释 `content`，只在保存与恢复之间原样交还。"""

    content: object


@dataclass(frozen=True)
class PaceState:
    """限速与急停里、服务端和 hook 都要看见的那一部分。

    间隔的计时只活在服务端进程内，不在这里。
    """

    streak: int = 0
    stopped: bool = False


class DesktopPort(Protocol):
    """桌面能提供的原始事实，以及两幅采集之间的像素比较。

    窗口是不是截图里的那个、观察过时了要不要拦截，仍由核心判定。
    逐像素比较只在这个接口的实现里做，核心只使用比较结果。
    """

    def list_windows(self) -> Sequence[Window]:
        """枚举所有顶层窗口，含不可见与最小化的，交由核心筛选。"""
        ...

    def capture_window(self, handle: int) -> Capture:
        """截取一个窗口本身，被其他窗口遮住的部分也照常画出。

        窗口已经不在时抛 `WindowUnavailable`。
        """
        ...

    def compare_region(self, before: Capture, after: Capture, x: int, y: int) -> RegionChange:
        """比较两幅采集在屏幕物理像素 `(x, y)` 周围目标区域里的像素。

        `before.rect` 与 `after.rect` 须相同。
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

    def double_click(self, x: int, y: int) -> None:
        """在屏幕物理像素 `(x, y)` 处双击鼠标左键。"""
        ...

    def right_click(self, x: int, y: int) -> None:
        """在屏幕物理像素 `(x, y)` 处单击鼠标右键。"""
        ...

    def drag(self, x: int, y: int, to_x: int, to_y: int) -> None:
        """按住左键从 `(x, y)` 拖到 `(to_x, to_y)`，坐标都是屏幕物理像素。"""
        ...

    def scroll(self, x: int, y: int, notches: int) -> None:
        """在 `(x, y)` 处滚动滚轮。`notches` 为正向上、为向下，一格是一次凹口。"""
        ...

    def press_keys(self, keys: Sequence[str]) -> None:
        """按顺序按下 `keys` 再逆序松开，组成一次组合键。名字由核心规范过。"""
        ...

    def launch(self, executable: str) -> int:
        """启动 `executable`，不经 shell、不附带参数，返回进程号。启动不了时抛 `LaunchError`。"""
        ...

    def focus(self, handle: int) -> None:
        """把窗口带到前台，使随后的键盘输入落进它。做不到时抛 `ForegroundError`。"""
        ...

    def read_clipboard(self) -> Clipboard:
        """当前剪贴板内容的快照。读不到时抛 `ClipboardUnavailable`。"""
        ...

    def set_clipboard_text(self, text: str) -> None:
        """用一段文本替换剪贴板内容。写不进去时抛 `ClipboardUnavailable`，剪贴板保持原样。"""
        ...

    def restore_clipboard(self, snapshot: Clipboard) -> None:
        """把剪贴板放回 `read_clipboard` 给出的快照。放不回去时抛 `ClipboardUnavailable`。"""
        ...

    def paste(self) -> None:
        """向当前前台窗口粘贴（Ctrl+V）。送不出去时抛 `ClipboardUnavailable`。"""
        ...

    def type_character(self, character: str) -> None:
        """向当前前台窗口注入一个 Unicode 字符。送不出去时抛 `InjectionError`。"""
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

    def read_pace(self) -> PaceState:
        """连续输入次数，以及是否正处于急停。还没有记录时连续次数为 0、未急停。"""
        ...

    def write_pace(self, state: PaceState) -> None:
        """记下连续输入次数与急停。服务端写，hook 读，两边是不同进程。"""
        ...

    def register_stop_hotkey(self, on_stop: Callable[[], None]) -> None:
        """注册全局急停热键 Ctrl+Break。按下时调用 `on_stop`。重复注册只更换回调。"""
        ...

    def path_kind(self, path: str) -> Literal["file", "dir"] | None:
        """路径是文件、目录，还是不存在。"""
        ...

    def write_text(self, path: str, content: str) -> None:
        """把文本写入文件，父目录须已存在。写不进去时抛 `FileError`。"""
        ...

    def delete_path(self, path: str, *, permanent: bool) -> None:
        """删除路径。`permanent` 为假时移入回收站，为真时永久删除。删不掉时抛 `FileError`。"""
        ...

    def move_path(self, source: str, destination: str) -> None:
        """把文件或目录挪到新路径。目标已是文件时覆盖它。挪不动时抛 `FileError`。"""
        ...

    def read_text(self, path: str) -> str:
        """读出文本文件的内容。路径不存在、或它是目录时抛 `FileError`。"""
        ...

    def list_dir(self, path: str) -> Sequence[DirEntry]:
        """列出目录的直接子项，按名字排序。路径不是目录时抛 `FileError`。"""
        ...

    def run_powershell(self, command: str) -> CommandResult:
        """执行一条 PowerShell 命令，不加载配置文件。

        返回标准输出、标准错误与退出码。进程起不来或超时抛 `CommandError`。
        """
        ...

    def confirm(
        self, *, title: str, message: str, image: Image | None, timeout: float
    ) -> bool | None:
        """弹出系统原生确认对话框，模型无法参与。

        人点允许返回 `True`，点拒绝或关掉窗口返回 `False`，
        `timeout` 秒内没有回应返回 `None`。`image` 是要一并呈现的窗口截图，没有时为 `None`。
        """
        ...
