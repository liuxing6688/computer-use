"""`DesktopPort` 的测试替身：持有一份可脚本化的窗口布局与预置截图。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Collection, Literal, Mapping, Sequence

from PIL import Image

from computer_use.desktop import (
    Capture,
    Clipboard,
    ClipboardUnavailable,
    CommandError,
    CommandResult,
    DirEntry,
    FileError,
    ForegroundError,
    InjectionError,
    LaunchError,
    PaceState,
    Rect,
    TextUnreadable,
    Window,
    WindowUnavailable,
)


@dataclass(frozen=True)
class ShownDialog:
    """测试替身记下的一次原生确认：人还没点，内容已经摆好。"""

    title: str
    message: str
    image: Image.Image | None
    timeout: float


class FakeDesktop:
    """按给定的窗口布局回答枚举、截图与命中测试，不需要真实桌面。

    `windows` 的顺序即 Z 序，排在前面的在上层。没有预置截图的窗口截出来是一张与窗口等大的白图。
    `gone` 中的窗口仍会被枚举出来，但截图时已经关掉了。`agent_processes` 是 Agent 自身所在的进程。
    `texts` 是屏幕上写着的文字及其屏幕矩形，文字识别读出中心落在识别区域内的那些；
    `unreadable` 为真时文字识别无法进行。
    注入的点击、文本、剪贴板、动作日志、留证截图与裁决凭据都留在内存里，可供断言。
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
        files: Mapping[str, str | None] | None = None,
    ) -> None:
        self._windows = list(windows)
        self._images = dict(images or {})
        self._dpi_scale = dpi_scale
        self._gone = set(gone)
        self._agent_processes = frozenset(agent_processes)
        self._texts = list(texts)
        self._unreadable = unreadable
        self.clicks: list[tuple[int, int]] = []
        self.double_clicks: list[tuple[int, int]] = []
        self.right_clicks: list[tuple[int, int]] = []
        self.drags: list[tuple[int, int, int, int]] = []
        self.scrolls: list[tuple[int, int, int]] = []
        self.chords: list[tuple[str, ...]] = []
        self.launched: list[str] = []
        self.spawn: list[Window] = []
        self.launch_pid = 4242
        self.launch_error: str | None = None
        self.log_lines: list[str] = []
        self.evidence: dict[str, bytes] = {}
        self.tickets: dict[str, datetime] = {}
        self._pace_state = PaceState()
        self.stop_hotkey: Callable[[], None] | None = None
        self.clipboard: object = ""
        self.pasted: list[object] = []
        self.characters: list[str] = []
        self.trace: list[tuple[object, ...]] = []
        self.dialogs: list[ShownDialog] = []
        self.dialog_reply: bool | None = True
        self._files = {_normalize_path(path): content for path, content in (files or {}).items()}
        self.recycled: list[str] = []
        self.deleted: list[str] = []
        self.commands: list[str] = []
        self.command_result = CommandResult(stdout="", stderr="", exit_code=0)
        self.command_error: str | None = None
        self.focus_fails = False
        self.clipboard_read_fails = False
        self.clipboard_write_fails = False
        self.clipboard_restore_fails = False
        self.paste_fails = False
        self.unicode_fails = False

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

    def double_click(self, x: int, y: int) -> None:
        self.double_clicks.append((x, y))

    def right_click(self, x: int, y: int) -> None:
        self.right_clicks.append((x, y))

    def drag(self, x: int, y: int, to_x: int, to_y: int) -> None:
        self.drags.append((x, y, to_x, to_y))

    def scroll(self, x: int, y: int, notches: int) -> None:
        self.scrolls.append((x, y, notches))

    def press_keys(self, keys: Sequence[str]) -> None:
        self.chords.append(tuple(keys))

    def launch(self, executable: str) -> int:
        self.launched.append(executable)
        if self.launch_error is not None:
            raise LaunchError(self.launch_error)
        self._windows[0:0] = list(self.spawn)
        self.spawn = []
        return self.launch_pid

    def add_window(self, extra: Window) -> None:
        self._windows.insert(0, extra)

    def focus(self, handle: int) -> None:
        self.trace.append(("focus", handle))
        if self.focus_fails:
            raise ForegroundError(f"窗口 {handle} 没能来到前台")

    def read_clipboard(self) -> Clipboard:
        self.trace.append(("read_clipboard",))
        if self.clipboard_read_fails:
            raise ClipboardUnavailable("剪贴板读不出来")
        return Clipboard(self.clipboard)

    def set_clipboard_text(self, text: str) -> None:
        self.trace.append(("set_clipboard_text", text))
        if self.clipboard_write_fails:
            raise ClipboardUnavailable("文本写不进剪贴板")
        self.clipboard = text

    def restore_clipboard(self, snapshot: Clipboard) -> None:
        self.trace.append(("restore_clipboard",))
        if self.clipboard_restore_fails:
            raise ClipboardUnavailable("原剪贴板内容没能恢复")
        self.clipboard = snapshot.content

    def paste(self) -> None:
        self.trace.append(("paste",))
        if self.paste_fails:
            raise ClipboardUnavailable("粘贴没能送进前台窗口")
        self.pasted.append(self.clipboard)

    def type_character(self, character: str) -> None:
        self.trace.append(("type_character", character))
        if self.unicode_fails:
            raise InjectionError(f"字符 {character!r} 没能送进前台窗口")
        self.characters.append(character)

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

    def read_pace(self) -> PaceState:
        return self._pace_state

    def write_pace(self, state: PaceState) -> None:
        self._pace_state = state

    def register_stop_hotkey(self, on_stop: Callable[[], None]) -> None:
        self.stop_hotkey = on_stop

    def path_kind(self, path: str) -> Literal["file", "dir"] | None:
        key = _normalize_path(path)
        if isinstance(self._files.get(key), str):
            return "file"
        if _is_dir(self._files, key):
            return "dir"
        return None

    def write_text(self, path: str, content: str) -> None:
        target = _normalize_path(path)
        parent = target.rsplit("/", 1)[0]
        if parent == target or not _is_dir(self._files, parent):
            raise FileError(f"写不进文件：{path}")
        if self._files.get(target) is None and target in self._files:
            raise FileError(f"写不进文件：{path}")
        self._files[target] = content

    def delete_path(self, path: str, *, permanent: bool) -> None:
        key = _normalize_path(path)
        if self.path_kind(path) is None:
            raise FileError(f"删不掉：{path}")
        prefix = key + "/"
        for existing in [item for item in self._files if item == key or item.startswith(prefix)]:
            del self._files[existing]
        (self.deleted if permanent else self.recycled).append(path)

    def move_path(self, source: str, destination: str) -> None:
        src = _normalize_path(source)
        dst = _normalize_path(destination)
        kind = self.path_kind(source)
        if kind is None:
            raise FileError(f"移不走：{source}")
        parent = dst.rsplit("/", 1)[0]
        if parent == dst or not _is_dir(self._files, parent):
            raise FileError(f"移不走：{source}")
        if self.path_kind(destination) == "dir":
            raise FileError(f"移不走：{source}")
        prefix = src + "/"
        moving = {
            key: content
            for key, content in self._files.items()
            if key == src or key.startswith(prefix)
        }
        for key in moving:
            del self._files[key]
        for key, content in moving.items():
            self._files[dst + key[len(src) :]] = content

    def read_text(self, path: str) -> str:
        content = self._files.get(_normalize_path(path))
        if not isinstance(content, str):
            raise FileError(f"读不到文件：{path}")
        return content

    def list_dir(self, path: str) -> Sequence[DirEntry]:
        root = _normalize_path(path)
        if not _is_dir(self._files, root):
            raise FileError(f"不是目录：{path}")
        prefix = root + "/"
        children: dict[str, bool] = {}
        for key, content in self._files.items():
            if not key.startswith(prefix):
                continue
            name = key[len(prefix) :].split("/", 1)[0]
            rest = key[len(prefix) + len(name) :]
            children[name] = rest != "" or content is None
        return tuple(DirEntry(name, is_dir) for name, is_dir in sorted(children.items()))

    def run_powershell(self, command: str) -> CommandResult:
        self.commands.append(command)
        if self.command_error is not None:
            raise CommandError(self.command_error)
        return self.command_result

    def confirm(
        self, *, title: str, message: str, image: Image.Image | None, timeout: float
    ) -> bool | None:
        self.dialogs.append(ShownDialog(title=title, message=message, image=image, timeout=timeout))
        return self.dialog_reply

    def press_stop_hotkey(self) -> None:
        if self.stop_hotkey is None:
            raise AssertionError("还没有注册急停热键")
        self.stop_hotkey()

    def action_log(self) -> list[dict[str, Any]]:
        """动作日志逐行解析后的记录。"""

        return [json.loads(line) for line in self.log_lines]


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").rstrip("/")


def _is_dir(files: Mapping[str, str | None], path: str) -> bool:
    if files.get(path) is None and path in files:
        return True
    prefix = path + "/"
    return any(key.startswith(prefix) for key in files)


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
