"""`DesktopPort` 的测试替身：持有一份可脚本化的窗口布局与预置截图。"""

from __future__ import annotations

import json
from typing import Any, Collection, Mapping, Sequence

from PIL import Image

from computer_use.desktop import Capture, Rect, Window, WindowUnavailable


class FakeDesktop:
    """按给定的窗口布局回答枚举，按预置截图回答截图，不需要真实桌面。

    没有预置截图的窗口截出来是一张与窗口等大的白图。`gone` 中的窗口仍会被枚举出来，
    但截图时已经关掉了。动作日志与留证截图都留在内存里，按位置可查。
    """

    def __init__(
        self,
        windows: Sequence[Window] = (),
        *,
        images: Mapping[int, Image.Image] | None = None,
        dpi_scale: float = 1.0,
        gone: Collection[int] = (),
    ) -> None:
        self._windows = list(windows)
        self._images = dict(images or {})
        self._dpi_scale = dpi_scale
        self._gone = set(gone)
        self.log_lines: list[str] = []
        self.evidence: dict[str, bytes] = {}

    def list_windows(self) -> Sequence[Window]:
        return tuple(self._windows)

    def capture_window(self, handle: int) -> Capture:
        window = next((w for w in self._windows if w.handle == handle), None)
        if window is None or handle in self._gone:
            raise WindowUnavailable(handle)
        size = (window.rect.width, window.rect.height)
        image = self._images.get(handle) or Image.new("RGB", size, "white")
        assert image.size == size, "预置截图须与窗口矩形等大"
        return Capture(image=image, rect=window.rect, dpi_scale=self._dpi_scale)

    def append_log(self, line: str) -> None:
        assert "\n" not in line, "一条日志须是一行"
        self.log_lines.append(line)

    def save_evidence(self, png: bytes) -> str:
        location = f"evidence/{len(self.evidence) + 1}.png"
        self.evidence[location] = png
        return location

    def action_log(self) -> list[dict[str, Any]]:
        """动作日志逐行解析后的记录。"""

        return [json.loads(line) for line in self.log_lines]


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
