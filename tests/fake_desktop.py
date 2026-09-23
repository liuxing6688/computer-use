"""`DesktopPort` 的测试替身：持有一份可脚本化的窗口布局与预置截图。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Collection, Mapping, Sequence

from PIL import Image

from computer_use.desktop import Capture, Rect, TextUnreadable, Window, WindowUnavailable


class FakeDesktop:
    """按给定的窗口布局回答枚举、截图与命中测试，不需要真实桌面。

    `windows` 的顺序即 Z 序，排在前面的在上层。没有预置截图的窗口截出来是一张与窗口等大的白图。
    `gone` 中的窗口仍会被枚举出来，但截图时已经关掉了。`agent_processes` 是 Agent 自身所在的进程。
    `texts` 是屏幕上写着的文字及其屏幕矩形，文字识别读出中心落在识别区域内的那些；
    `unreadable` 为真时文字识别无法进行。
    注入的点击、动作日志、留证截图与裁决凭据都留在内存里，可供断言。
    """

    def __init__(
        self,
        windows: Sequence[Window] = (),
        *,
        images: Mapping[int, Image.Image] | None = None,
        dpi_scale: float = 1.0,
        gone: Collection[int] = (),
        agent_processes: Collection[int] = (),
        texts: Sequence[tuple[Rect, str]] = (),
        unreadable: bool = False,
    ) -> None:
        self._windows = list(windows)
        self._images = dict(images or {})
        self._dpi_scale = dpi_scale
        self._gone = set(gone)
        self._agent_processes = frozenset(agent_processes)
        self._texts = list(texts)
        self._unreadable = unreadable
        self.clicks: list[tuple[int, int]] = []
        self.log_lines: list[str] = []
        self.evidence: dict[str, bytes] = {}
        self.tickets: dict[str, datetime] = {}

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

    def window_at(self, x: int, y: int) -> int | None:
        for w in self._windows:
            r = w.rect
            if (
                w.is_visible
                and not w.is_minimized
                and w.handle not in self._gone
                and r.left <= x < r.left + r.width
                and r.top <= y < r.top + r.height
            ):
                return w.handle
        return None

    def agent_process_ids(self) -> Collection[int]:
        return self._agent_processes

    def recognize_text(self, capture: Capture, region: Rect) -> str:
        if self._unreadable:
            raise TextUnreadable("没有可用的识别语言")
        c = capture.rect
        assert (
            c.left <= region.left
            and c.top <= region.top
            and region.left + region.width <= c.left + c.width
            and region.top + region.height <= c.top + c.height
        ), "识别区域须在截图之内"
        return " ".join(
            text
            for rect, text in self._texts
            if region.left <= rect.left + rect.width / 2 < region.left + region.width
            and region.top <= rect.top + rect.height / 2 < region.top + region.height
        )

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def append_log(self, line: str) -> None:
        assert "\n" not in line, "一条日志须是一行"
        self.log_lines.append(line)

    def save_evidence(self, png: bytes) -> str:
        location = f"evidence/{len(self.evidence) + 1}.png"
        self.evidence[location] = png
        return location

    def put_ticket(self, key: str, issued_at: datetime) -> None:
        self.tickets[key] = issued_at

    def take_ticket(self, key: str) -> datetime | None:
        return self.tickets.pop(key, None)

    def action_log(self) -> list[dict[str, Any]]:
        """动作日志逐行解析后的记录。"""

        return [json.loads(line) for line in self.log_lines]


def window(
    *,
    handle: int = 1,
    title: str = "无标题 - 记事本",
    process_id: int | None = None,
    process_name: str = "notepad.exe",
    rect: Rect = Rect(left=0, top=0, width=800, height=600),
    is_visible: bool = True,
    is_minimized: bool = False,
    owner: int | None = None,
) -> Window:
    """构造一个窗口，默认是一个可见、有标题的普通窗口，各自属于一个与句柄同号的进程。"""

    return Window(
        handle=handle,
        title=title,
        process_id=handle if process_id is None else process_id,
        process_name=process_name,
        rect=rect,
        is_visible=is_visible,
        is_minimized=is_minimized,
        owner=owner,
    )
