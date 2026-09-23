"""工具层：把核心的观察结果整理成可回传给模型的形状。

这里不做判定，判定在 `windows.py`。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Sequence

from computer_use import actions, observation
from computer_use.desktop import DesktopPort, Rect, Window
from computer_use.observation import Screenshot, Screenshots
from computer_use.scope import TaskScope
from computer_use.windows import operable_windows


@dataclass(frozen=True)
class Observed:
    """一张截图的 PNG 编码与它的元数据。"""

    png: bytes
    metadata: dict[str, Any]


def list_windows(desktop: DesktopPort) -> list[dict[str, Any]]:
    """只读工具：列出桌面上可操作的窗口。"""

    return [_as_payload(w) for w in operable_windows(desktop)]


def observe_window(
    desktop: DesktopPort, screenshots: Screenshots, handle: int
) -> Observed:
    """只读工具：截取一个窗口，附带把截图坐标换算回屏幕所需的元数据。"""

    return _as_observed(observation.observe(desktop, screenshots, handle))


def zoom(screenshots: Screenshots, screenshot_id: str, rect: Rect) -> Observed:
    """只读工具：把截图上的一块矩形按原尺寸放大，附带同样的元数据。"""

    return _as_observed(observation.zoom(screenshots, screenshot_id, rect))


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
) -> dict[str, Any]:
    """输入工具：在某张截图的像素 `(x, y)` 处单击，返回实际落点。"""

    landed = actions.click(
        desktop, screenshots, scope, screenshot_id, x, y, intent=intent, dangerous=dangerous
    )
    return {
        "window": _as_identity(landed.window),
        "screen_point": {"x": landed.x, "y": landed.y},
    }


def _as_identity(window: Window) -> dict[str, Any]:
    return {
        "handle": window.handle,
        "title": window.title,
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
