"""工具层：把核心的观察结果整理成可回传给模型的形状。

这里不做判定，判定在 `windows.py`。
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from computer_use import actions, files, observation, powershell
from computer_use.desktop import DesktopPort, Rect, Window
from computer_use.observation import Screenshot, Screenshots
from computer_use.pace import Pace
from computer_use.scope import TaskScope
from computer_use.untrusted import wrap_untrusted
from computer_use.windows import operable_windows


@dataclass(frozen=True)
class Observed:
    """一张截图的 PNG 编码与它的元数据。"""

    png: bytes
    metadata: dict[str, Any]


def list_windows(desktop: DesktopPort) -> list[dict[str, Any]]:
    """只读工具：列出桌面上可操作的窗口。"""

    return [_as_payload(w) for w in operable_windows(desktop)]


def read_file(desktop: DesktopPort, path: str) -> dict[str, Any]:
    """只读工具：读出文本文件的内容。不经确认。"""

    return {"path": path, "content": desktop.read_text(path)}


def list_directory(desktop: DesktopPort, path: str) -> list[dict[str, Any]]:
    """只读工具：列出目录的直接子项。不经确认。"""

    return [{"name": entry.name, "is_dir": entry.is_dir} for entry in desktop.list_dir(path)]


def write_file(
    desktop: DesktopPort,
    path: str,
    content: str,
    *,
    intent: str,
    dangerous: bool,
) -> dict[str, Any]:
    """把文本写入文件。一律须经人裁决，覆盖与新建走同一道拦截。"""

    return files.write_file(desktop, path, content, intent=intent, dangerous=dangerous)


def move_file(
    desktop: DesktopPort,
    source: str,
    destination: str,
    *,
    intent: str,
    dangerous: bool,
) -> dict[str, Any]:
    """移动文件或目录。一律须经人裁决。目标已是文件时确认为覆盖。"""

    return files.move_file(
        desktop, source, destination, intent=intent, dangerous=dangerous
    )


def delete_file(
    desktop: DesktopPort,
    path: str,
    *,
    intent: str,
    dangerous: bool,
    permanent: bool | None = None,
) -> dict[str, Any]:
    """删除文件或目录。默认移入回收站；永久删除须显式请求。一律须经人裁决。"""

    return files.delete_file(
        desktop, path, intent=intent, dangerous=dangerous, permanent=permanent
    )


def observe_window(
    desktop: DesktopPort, screenshots: Screenshots, handle: int
) -> Observed:
    """只读工具：截取一个窗口，附带把截图坐标换算回屏幕所需的元数据。"""

    return _as_observed(observation.observe(desktop, screenshots, handle))


def zoom(screenshots: Screenshots, screenshot_id: str, rect: Rect) -> Observed:
    """只读工具：把截图上的一块矩形按原尺寸放大，附带同样的元数据。"""

    return _as_observed(observation.zoom(screenshots, screenshot_id, rect))


def run_powershell(
    desktop: DesktopPort,
    command: str,
    *,
    intent: str,
    dangerous: bool,
) -> dict[str, Any]:
    """执行一条 PowerShell 命令并返回输出。只读直接执行；写操作、下载与动态求值须经人裁决。"""

    return powershell.run_powershell(desktop, command, intent=intent, dangerous=dangerous)


def declare_scope(
    desktop: DesktopPort, scope: TaskScope, handles: Sequence[int]
) -> list[dict[str, Any]]:
    """声明本次任务涉及的窗口，替换原有的任务作用域，返回声明后的作用域。"""

    scope.declare(desktop, handles)
    return get_scope(scope)


def get_scope(scope: TaskScope) -> list[dict[str, Any]]:
    """只读工具：当前的任务作用域，每个窗口记的是声明时的样子。"""

    return [_as_identity(w) for w in scope.windows]


def click(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> dict[str, Any]:
    """输入工具：在某张截图的像素 `(x, y)` 处单击，返回实际落点。"""

    landed = actions.click(
        desktop,
        screenshots,
        scope,
        screenshot_id,
        x,
        y,
        intent=intent,
        dangerous=dangerous,
        pace=pace,
    )
    return _as_landed(landed)


def double_click(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> dict[str, Any]:
    """输入工具：在某张截图的像素 `(x, y)` 处双击，返回实际落点。"""

    return _as_landed(
        actions.double_click(
            desktop, screenshots, scope, screenshot_id, x, y,
            intent=intent, dangerous=dangerous, pace=pace,
        )
    )


def right_click(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> dict[str, Any]:
    """输入工具：在某张截图的像素 `(x, y)` 处单击右键，返回实际落点。"""

    return _as_landed(
        actions.right_click(
            desktop, screenshots, scope, screenshot_id, x, y,
            intent=intent, dangerous=dangerous, pace=pace,
        )
    )


def drag(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
    to_x: int,
    to_y: int,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> dict[str, Any]:
    """输入工具：从截图像素 `(x, y)` 拖到 `(to_x, to_y)`，返回两端的实际落点。"""

    dragged = actions.drag(
        desktop, screenshots, scope, screenshot_id, x, y, to_x, to_y,
        intent=intent, dangerous=dangerous, pace=pace,
    )
    return {**_as_landed(dragged.start), "to": _as_landed(dragged.end)}


def scroll(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
    notches: int,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> dict[str, Any]:
    """输入工具：在某张截图的像素 `(x, y)` 处滚动，返回实际落点与格数。"""

    landed = actions.scroll(
        desktop, screenshots, scope, screenshot_id, x, y, notches,
        intent=intent, dangerous=dangerous, pace=pace,
    )
    return {**_as_landed(landed), "notches": notches}


def press_keys(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    keys: Sequence[str],
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> dict[str, Any]:
    """输入工具：把组合键送进某张截图所属的窗口。"""

    pressed = actions.press_keys(
        desktop, screenshots, scope, screenshot_id, keys,
        intent=intent, dangerous=dangerous, pace=pace,
    )
    return {"window": _as_identity(pressed.window), "keys": list(pressed.keys)}


def launch_app(
    desktop: DesktopPort,
    app: str,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
    timeout: float = 15,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """输入工具：启动一个应用并等到它的新窗口。新窗口不进入任务作用域。"""

    launched = actions.launch_app(
        desktop, app,
        intent=intent, dangerous=dangerous, pace=pace, timeout=timeout, clock=clock, sleep=sleep,
    )
    return {"window": _as_identity(launched.window), "process_id": launched.process_id}


def type_text(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    text: str,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> dict[str, Any]:
    """输入工具：把文本打进某张截图所属窗口的焦点输入框。

    `tier` 是实际走通的那一档：`clipboard` 为剪贴板粘贴，`unicode` 为逐字符注入。
    `clipboard_used` 为真表示这段文本曾经写入剪贴板（调用结束时原内容已恢复，除非恢复本身失败）。
    """

    typed = actions.type_text(
        desktop,
        screenshots,
        scope,
        screenshot_id,
        text,
        intent=intent,
        dangerous=dangerous,
        pace=pace,
    )
    return {
        "window": _as_identity(typed.window),
        "tier": typed.tier,
        "clipboard_used": typed.clipboard_used,
    }


def resume(desktop: DesktopPort, pace: Pace) -> str:
    """显式恢复：解除急停，使输入工具重新可用。未急停时无事发生。"""

    return actions.resume(desktop, pace)


def _as_landed(landed: actions.Landed) -> dict[str, Any]:
    return {
        "window": _as_identity(landed.window),
        "screen_point": {"x": landed.x, "y": landed.y},
    }


def _as_identity(window: Window) -> dict[str, Any]:
    return {
        "handle": window.handle,
        "title": wrap_untrusted(window.title),
        "process_name": window.process_name,
    }


def _as_observed(screenshot: Screenshot) -> Observed:
    png = io.BytesIO()
    screenshot.image.save(png, format="PNG")
    window_x, window_y = screenshot.window_offset
    return Observed(
        png=png.getvalue(),
        metadata={
            "screenshot_id": screenshot.id,
            "window": _as_identity(screenshot.window),
            "captured_at": screenshot.captured_at.isoformat(),
            "size": {"width": screenshot.image.width, "height": screenshot.image.height},
            "scale": screenshot.scale,
            "dpi_scale": screenshot.capture.dpi_scale,
            "screen_offset": {"x": screenshot.region.left, "y": screenshot.region.top},
            "window_offset": {"x": window_x, "y": window_y},
        },
    )


def _as_payload(window: Window) -> dict[str, Any]:
    return {
        **_as_identity(window),
        "rect": {
            "left": window.rect.left,
            "top": window.rect.top,
            "width": window.rect.width,
            "height": window.rect.height,
        },
    }
