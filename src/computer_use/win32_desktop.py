"""`DesktopPort` 的 Windows 实现。除本模块外，仓库任何地方都不应 import Win32 库。"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Collection, Literal, Sequence, TypeGuard, TypeVar, cast

import win32clipboard
import win32con
import win32gui
import win32process
import win32ui
from PIL import Image, ImageChops
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter

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
    RegionChange,
    TextUnreadable,
    Window,
    WindowUnavailable,
)

_T = TypeVar("_T")
_ClipData = str | bytes | tuple[str, ...]
_ClipFormat = tuple[int, _ClipData]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_PW_RENDERFULLCONTENT = 0x2
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_EXTENDED_MAX_PATH = 32768

_TARGET_RADIUS = 48
"""目标区域：落点上下左右各这么多物理像素，界面变化只在这块区域里才算数。"""

_PIXEL_TOLERANCE = 32
"""一个像素任一通道的差不超过此值时视为没变，悬停一类的轻微变色不算。"""

_CHANGED_FRACTION = 0.02
"""目标区域里变了的像素超过这个比例才算实质变化；闪烁的光标只占百分之一不到。"""


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
)
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetShellWindow.restype = wintypes.HWND
_user32.GetShellWindow.argtypes = ()
_user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
_user32.SetProcessDpiAwarenessContext.argtypes = (wintypes.HANDLE,)
_user32.GetThreadDpiAwarenessContext.restype = wintypes.HANDLE
_user32.GetThreadDpiAwarenessContext.argtypes = ()
_user32.AreDpiAwarenessContextsEqual.restype = wintypes.BOOL
_user32.AreDpiAwarenessContextsEqual.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
_user32.GetDpiForWindow.restype = wintypes.UINT
_user32.GetDpiForWindow.argtypes = (wintypes.HWND,)
_user32.PrintWindow.restype = wintypes.BOOL
_user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)

_user32.WindowFromPoint.restype = wintypes.HWND
_user32.WindowFromPoint.argtypes = (wintypes.POINT,)
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
_user32.SetCursorPos.restype = wintypes.BOOL
_user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
_user32.keybd_event.argtypes = (wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t)
_user32.keybd_event.restype = None
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.GetCurrentThreadId.argtypes = ()
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
_user32.SetTimer.restype = ctypes.c_size_t
_user32.SetTimer.argtypes = (wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p)
_user32.LoadCursorW.restype = wintypes.HANDLE
_user32.LoadCursorW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT))


class _INPUT(ctypes.Structure):
    """`MOUSEINPUT` 是联合体里最大的成员，键盘事件放在同一块里。"""

    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


_user32.SendInput.restype = wintypes.UINT
_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
_user32.GetMessageW.restype = ctypes.c_int
_user32.GetMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
)

_MOD_CONTROL = 0x0002
_VK_CANCEL = 0x03
_WM_HOTKEY = 0x0312
_STOP_HOTKEY_ID = 1


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    )


_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))

_GA_ROOT = 2
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_WHEEL = 0x0800
_WHEEL_DELTA = 120
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_VK_CONTROL = 0x11
_VK_V = 0x56
# Ctrl+V 送出后留给前台窗口读剪贴板的时间，读完调用方才能把原内容放回去。
_PASTE_SETTLE_SECONDS = 0.2
_TH32CS_SNAPPROCESS = 0x2
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
_dwmapi.DwmGetWindowAttribute.restype = ctypes.HRESULT
_dwmapi.DwmGetWindowAttribute.argtypes = (
    wintypes.HWND,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
)


class Win32Desktop:
    """用 Win32 API 采集真实桌面的事实。

    构造即声明 `PER_MONITOR_AWARE_V2`，因此窗口矩形、命中测试与鼠标坐标一律是物理像素。
    动作日志写在 `data_dir/actions.jsonl`，留证截图存在 `data_dir/evidence/`，
    裁决凭据存在 `data_dir/tickets/`（hook 与服务端各用一个 `Win32Desktop`，靠这个目录交接）；
    `data_dir` 缺省为 `%LOCALAPPDATA%\\computer-use`。
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        _declare_dpi_awareness()
        self._data_dir = data_dir or Path(os.environ["LOCALAPPDATA"]) / "computer-use"
        self._agent_processes = _ancestry(os.getpid())

    def list_windows(self) -> Sequence[Window]:
        windows: list[Window] = []
        win32gui.EnumWindows(lambda handle, _: _collect(handle, windows), None)
        return tuple(windows)

    def capture_window(self, handle: int) -> Capture:
        try:
            left, top, right, bottom = win32gui.GetWindowRect(handle)
            image = _print_window(handle, right - left, bottom - top)
            frame = _extended_frame_bounds(handle)
        except (win32gui.error, win32ui.error) as error:
            raise WindowUnavailable(handle) from error
        image = image.crop(
            (
                frame.left - left,
                frame.top - top,
                frame.left - left + frame.width,
                frame.top - top + frame.height,
            )
        )
        return Capture(
            image=image, rect=frame, dpi_scale=_user32.GetDpiForWindow(handle) / 96
        )

    def compare_region(self, before: Capture, after: Capture, x: int, y: int) -> RegionChange:
        rect = before.rect
        box = (
            max(x - _TARGET_RADIUS, rect.left) - rect.left,
            max(y - _TARGET_RADIUS, rect.top) - rect.top,
            min(x + _TARGET_RADIUS + 1, rect.left + rect.width) - rect.left,
            min(y + _TARGET_RADIUS + 1, rect.top + rect.height) - rect.top,
        )
        original = before.image.convert("RGB").crop(box)
        current = after.image.convert("RGB").crop(box)
        red, green, blue = ImageChops.difference(original, current).split()
        largest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        changed = sum(largest.histogram()[_PIXEL_TOLERANCE + 1 :])
        total = original.width * original.height
        return RegionChange(
            changed=changed > _CHANGED_FRACTION * total,
            changed_pixels=changed,
            total_pixels=total,
        )

    def foreground_window(self) -> int | None:
        handle = win32gui.GetForegroundWindow()
        return int(handle) if handle else None

    def window_at(self, x: int, y: int) -> int | None:
        child = _user32.WindowFromPoint(wintypes.POINT(x, y))
        if not child:
            return None
        root: int | None = _user32.GetAncestor(child, _GA_ROOT)
        return root or None

    def agent_process_ids(self) -> Collection[int]:
        return self._agent_processes

    def recognize_text(self, capture: Capture, region: Rect) -> str:
        """用 `Windows.Media.Ocr` 把区域读一遍，每种装了的识别语言各读一遍，读数拼在一起。

        按用户语言挑一种不够：英文引擎把「删除」读成乱码，中文引擎读英文却没问题，
        界面语言与用户语言也未必一致。小区域放大一倍再读，9pt 的界面字才读得准。
        """

        image = capture.image.crop(
            (
                region.left - capture.rect.left,
                region.top - capture.rect.top,
                region.left - capture.rect.left + region.width,
                region.top - capture.rect.top + region.height,
            )
        ).resize((region.width * 2, region.height * 2), Image.Resampling.LANCZOS)
        with ThreadPoolExecutor(max_workers=1) as worker:
            return worker.submit(asyncio.run, _recognize(image)).result()

    def click(self, x: int, y: int) -> None:
        """把光标移到 `(x, y)` 再按下、抬起左键。

        调用方须已把目标窗口带到前台。鼠标事件送往光标下的窗口。
        """

        _place(x, y)
        _send(_mouse(_MOUSEEVENTF_LEFTDOWN), _mouse(_MOUSEEVENTF_LEFTUP))

    def double_click(self, x: int, y: int) -> None:
        _place(x, y)
        _send(
            _mouse(_MOUSEEVENTF_LEFTDOWN),
            _mouse(_MOUSEEVENTF_LEFTUP),
            _mouse(_MOUSEEVENTF_LEFTDOWN),
            _mouse(_MOUSEEVENTF_LEFTUP),
        )

    def right_click(self, x: int, y: int) -> None:
        _place(x, y)
        _send(_mouse(_MOUSEEVENTF_RIGHTDOWN), _mouse(_MOUSEEVENTF_RIGHTUP))

    def drag(self, x: int, y: int, to_x: int, to_y: int) -> None:
        _place(x, y)
        _send(_mouse(_MOUSEEVENTF_LEFTDOWN))
        try:
            _place(to_x, to_y)
        finally:
            _send(_mouse(_MOUSEEVENTF_LEFTUP))

    def scroll(self, x: int, y: int, notches: int) -> None:
        _place(x, y)
        _send(_mouse(_MOUSEEVENTF_WHEEL, notches * _WHEEL_DELTA))

    def press_keys(self, keys: Sequence[str]) -> None:
        virtual = [_virtual_key(key) for key in keys]
        _send(
            *(_key(code, 0) for code in virtual),
            *(_key(code, _KEYEVENTF_KEYUP) for code in reversed(virtual)),
        )

    def launch(self, executable: str) -> int:
        try:
            process = subprocess.Popen([executable])
        except OSError as error:
            raise LaunchError(f"启动不了 {executable}：{error}") from error
        return int(process.pid)

    def focus(self, handle: int) -> None:
        if not win32gui.IsWindow(handle):
            raise ForegroundError(f"窗口 {handle} 已经不在了")
        if win32gui.IsIconic(handle):
            win32gui.ShowWindow(handle, win32con.SW_RESTORE)
        _force_foreground(handle)
        if win32gui.GetForegroundWindow() != handle:
            raise ForegroundError(f"窗口 {handle} 没能来到前台")

    def read_clipboard(self) -> Clipboard:
        try:
            return Clipboard(_read_formats())
        except OSError as error:
            raise ClipboardUnavailable("剪贴板读不出来") from error

    def set_clipboard_text(self, text: str) -> None:
        previous = self.read_clipboard()
        try:
            _replace_clipboard(((win32con.CF_UNICODETEXT, text),))
        except ClipboardUnavailable:
            try:
                self.restore_clipboard(previous)
            except ClipboardUnavailable:
                raise ClipboardUnavailable("文本写不进剪贴板，而且原内容没能放回去") from None
            raise

    def restore_clipboard(self, snapshot: Clipboard) -> None:
        _replace_clipboard(_as_formats(snapshot.content))

    def paste(self) -> None:
        """向当前前台窗口粘贴。按键送出后稍等，前台窗口才来得及把剪贴板读走。"""

        try:
            _send(
                _key(_VK_CONTROL, 0),
                _key(_VK_V, 0),
                _key(_VK_V, _KEYEVENTF_KEYUP),
                _key(_VK_CONTROL, _KEYEVENTF_KEYUP),
            )
        except OSError as error:
            raise ClipboardUnavailable("粘贴没能送进前台窗口") from error
        time.sleep(_PASTE_SETTLE_SECONDS)

    def type_character(self, character: str) -> None:
        if len(character) != 1:
            raise InjectionError("一次只能注入一个字符")
        try:
            events: list[_INPUT] = []
            encoded = character.encode("utf-16-le")
            for index in range(0, len(encoded), 2):
                unit = int.from_bytes(encoded[index : index + 2], "little")
                events.append(_unicode_key(unit, key_up=False))
                events.append(_unicode_key(unit, key_up=True))
            _send(*events)
        except OSError as error:
            raise InjectionError(f"字符 {character!r} 没能送进前台窗口") from error

    def append_log(self, line: str) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        with (self._data_dir / "actions.jsonl").open("a", encoding="utf-8") as log:
            log.write(line + "\n")

    def save_evidence(self, png: bytes) -> str:
        directory = self._data_dir / "evidence"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = directory / f"{stamp}-{secrets.token_hex(2)}.png"
        with path.open("xb") as evidence:
            evidence.write(png)
        return str(path)

    def put_ticket(self, key: str, issued_at: datetime) -> None:
        directory = self._data_dir / "tickets"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / key).write_text(issued_at.isoformat(), encoding="utf-8")

    def take_ticket(self, key: str) -> datetime | None:
        """先把凭据改名再读：改名是原子的，两个调用同时来取，只有一个能拿到。"""

        taken = self._data_dir / "tickets" / f"{key}.{secrets.token_hex(4)}.taken"
        try:
            (self._data_dir / "tickets" / key).rename(taken)
        except FileNotFoundError:
            return None
        try:
            return datetime.fromisoformat(taken.read_text(encoding="utf-8"))
        finally:
            taken.unlink()

    def read_pace(self) -> PaceState:
        try:
            data = json.loads((self._data_dir / "pace.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return PaceState()
        return PaceState(streak=int(data["streak"]), stopped=bool(data["stopped"]))

    def write_pace(self, state: PaceState) -> None:
        """先写临时文件再替换，hook 那边不会读到写了一半的内容。"""

        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._data_dir / "pace.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"streak": state.streak, "stopped": state.stopped}),
            encoding="utf-8",
        )
        temporary.replace(path)

    def register_stop_hotkey(self, on_stop: Callable[[], None]) -> None:
        _StopHotkey.install(on_stop)

    def path_kind(self, path: str) -> Literal["file", "dir"] | None:
        target = Path(path)
        if target.is_file():
            return "file"
        if target.is_dir():
            return "dir"
        return None

    def write_text(self, path: str, content: str) -> None:
        try:
            Path(path).write_text(content, encoding="utf-8")
        except OSError as error:
            raise FileError(f"写不进文件：{path}") from error

    def delete_path(self, path: str, *, permanent: bool) -> None:
        if permanent:
            _erase(path)
            return
        _recycle(path)

    def move_path(self, source: str, destination: str) -> None:
        try:
            Path(source).replace(destination)
        except OSError as error:
            raise FileError(f"移不走：{source}") from error

    def read_text(self, path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as error:
            raise FileError(f"读不到文件：{path}") from error

    def list_dir(self, path: str) -> Sequence[DirEntry]:
        try:
            entries = [
                DirEntry(name=child.name, is_dir=child.is_dir())
                for child in Path(path).iterdir()
            ]
        except OSError as error:
            raise FileError(f"不是目录：{path}") from error
        return tuple(sorted(entries, key=lambda entry: entry.name))

    def run_powershell(self, command: str) -> CommandResult:
        wrapped = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [Console]::OutputEncoding; "
            + command
        )
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", wrapped],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise CommandError("PowerShell 在 60 秒内没有结束") from error
        except OSError as error:
            raise CommandError("PowerShell 没能启动") from error
        return CommandResult(
            stdout=completed.stdout.decode("utf-8", errors="replace"),
            stderr=completed.stderr.decode("utf-8", errors="replace"),
            exit_code=completed.returncode,
        )

    def confirm(
        self, *, title: str, message: str, image: Image.Image | None, timeout: float
    ) -> bool | None:
        return _native_confirm(title, message, image, timeout)


class _StopHotkey:
    """进程里一条消息循环，接 Ctrl+Break。重复注册只换回调，热键只占一个。"""

    callback: Callable[[], None] | None = None
    _thread: threading.Thread | None = None
    _ready = threading.Event()
    _error: BaseException | None = None

    @classmethod
    def install(cls, on_stop: Callable[[], None]) -> None:
        cls.callback = on_stop
        if cls._thread is not None:
            return
        cls._thread = threading.Thread(target=cls._loop, name="computer-use-stop", daemon=True)
        cls._thread.start()
        if not cls._ready.wait(5):
            raise TimeoutError("急停热键没有在 5 秒内注册上")
        if cls._error is not None:
            raise cls._error

    @classmethod
    def _loop(cls) -> None:
        if not _user32.RegisterHotKey(None, _STOP_HOTKEY_ID, _MOD_CONTROL, _VK_CANCEL):
            cls._error = ctypes.WinError(ctypes.get_last_error())
            cls._ready.set()
            return
        cls._ready.set()
        message = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == _WM_HOTKEY and cls.callback is not None:
                cls.callback()


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400

_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_shell32.SHFileOperationW.argtypes = (ctypes.POINTER(_SHFILEOPSTRUCTW),)
_shell32.SHFileOperationW.restype = ctypes.c_int


def _recycle(path: str) -> None:
    """`SHFileOperation` 的 `FOF_ALLOWUNDO`：进回收站，而不是直接抹掉。"""

    source = ctypes.create_unicode_buffer(str(Path(path)) + "\0")
    operation = _SHFILEOPSTRUCTW(wFunc=_FO_DELETE, pFrom=ctypes.cast(source, wintypes.LPCWSTR))
    operation.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    code = _shell32.SHFileOperationW(ctypes.byref(operation))
    if code != 0 or operation.fAnyOperationsAborted:
        raise FileError(f"删不掉：{path}")


def _erase(path: str) -> None:
    target = Path(path)
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as error:
        raise FileError(f"删不掉：{path}") from error


def _place(x: int, y: int) -> None:
    """把光标精确移到 `(x, y)`。

    `SendInput` 的绝对坐标要归一化到 0–65535，取整会偏一个像素；`SetCursorPos` 是精确的。
    """

    if not _user32.SetCursorPos(x, y):
        raise ctypes.WinError(ctypes.get_last_error())
    cursor = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(cursor))
    if (cursor.x, cursor.y) != (x, y):
        raise OSError(f"光标没能移到 ({x}, {y})，停在了 ({cursor.x}, {cursor.y})")


def _send(*inputs: _INPUT) -> None:
    array = (_INPUT * len(inputs))(*inputs)
    if _user32.SendInput(len(array), array, ctypes.sizeof(_INPUT)) != len(array):
        raise ctypes.WinError(ctypes.get_last_error())


def _mouse(flags: int, data: int = 0) -> _INPUT:
    return _INPUT(
        type=_INPUT_MOUSE,
        u=_INPUTUNION(mi=_MOUSEINPUT(dwFlags=flags, mouseData=data & 0xFFFFFFFF)),
    )


_NAMED_KEYS = {
    "enter": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "ctrl": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
}


def _virtual_key(name: str) -> int:
    if name in _NAMED_KEYS:
        return _NAMED_KEYS[name]
    if len(name) == 1 and "a" <= name <= "z":
        return ord(name.upper())
    if len(name) == 1 and "0" <= name <= "9":
        return ord(name)
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        return 0x70 + int(name[1:]) - 1
    raise InjectionError(f"不认识的按键：{name}")


def _key(virtual_key: int, flags: int) -> _INPUT:
    return _INPUT(
        type=_INPUT_KEYBOARD,
        u=_INPUTUNION(ki=_KEYBDINPUT(wVk=virtual_key, dwFlags=flags)),
    )


def _unicode_key(unit: int, *, key_up: bool) -> _INPUT:
    flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if key_up else 0)
    return _INPUT(
        type=_INPUT_KEYBOARD,
        u=_INPUTUNION(ki=_KEYBDINPUT(wScan=unit, dwFlags=flags)),
    )


def _force_foreground(handle: int) -> None:
    """把 `handle` 带到前台。后台进程直接 `SetForegroundWindow` 会被系统拒绝，先挂到前台线程上。"""

    if win32gui.GetForegroundWindow() == handle:
        return
    current = _kernel32.GetCurrentThreadId()
    target_thread, _ = win32process.GetWindowThreadProcessId(handle)
    foreground = win32gui.GetForegroundWindow()
    foreground_thread = 0
    if foreground:
        foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground)

    def attach(thread: int, on: bool) -> bool:
        if not thread or thread == current:
            return False
        return bool(_user32.AttachThreadInput(current, thread, on))

    attached_target = attach(target_thread, True)
    attached_foreground = attach(foreground_thread, True)
    try:
        win32gui.ShowWindow(handle, win32con.SW_SHOW)
        win32gui.SetForegroundWindow(handle)
        win32gui.BringWindowToTop(handle)
    finally:
        if attached_foreground:
            attach(foreground_thread, False)
        if attached_target:
            attach(target_thread, False)
    if win32gui.GetForegroundWindow() == handle:
        return
    # 空按键让系统把「最近收到输入」算到本进程头上，否则后台进程带不来前台。
    _user32.keybd_event(0, 0, 0, 0)
    win32gui.SetForegroundWindow(handle)
    win32gui.BringWindowToTop(handle)


def _with_clipboard(body: Callable[[], _T]) -> _T:
    last: OSError | None = None
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard(None)
        except OSError as error:
            last = error
            time.sleep(0.02)
            continue
        try:
            return body()
        finally:
            win32clipboard.CloseClipboard()  # type: ignore[no-untyped-call]
    raise ClipboardUnavailable("剪贴板正被别的程序占用") from last


def _read_formats() -> tuple[_ClipFormat, ...]:
    def read() -> tuple[_ClipFormat, ...]:
        found: list[_ClipFormat] = []
        fmt = 0
        while True:
            fmt = int(win32clipboard.EnumClipboardFormats(fmt))
            if not fmt:
                break
            try:
                data: object = win32clipboard.GetClipboardData(fmt)
            except OSError:
                continue
            if isinstance(data, str):
                found.append((fmt, data))
            elif isinstance(data, bytes):
                found.append((fmt, data))
            elif isinstance(data, bytearray | memoryview):
                found.append((fmt, bytes(data)))
            elif isinstance(data, tuple) and all(isinstance(part, str) for part in data):
                found.append((fmt, tuple(part for part in data)))
        return tuple(found)

    return _with_clipboard(read)


def _replace_clipboard(formats: tuple[_ClipFormat, ...]) -> None:
    def write() -> None:
        win32clipboard.EmptyClipboard()  # type: ignore[no-untyped-call]
        for fmt, data in formats:
            win32clipboard.SetClipboardData(fmt, data)  # type: ignore[no-untyped-call]

    try:
        _with_clipboard(write)
    except OSError as error:
        raise ClipboardUnavailable("剪贴板写不进去") from error


def _as_formats(content: object) -> tuple[_ClipFormat, ...]:
    if not isinstance(content, tuple):
        raise ClipboardUnavailable("剪贴板快照无法恢复")
    formats: list[_ClipFormat] = []
    for item in content:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ClipboardUnavailable("剪贴板快照无法恢复")
        fmt, data = item
        if not isinstance(fmt, int) or not _is_clip_data(data):
            raise ClipboardUnavailable("剪贴板快照无法恢复")
        formats.append((fmt, data))
    return tuple(formats)


def _is_clip_data(data: object) -> TypeGuard[_ClipData]:
    if isinstance(data, (str, bytes)):
        return True
    return isinstance(data, tuple) and all(isinstance(part, str) for part in data)


async def _recognize(image: Image.Image) -> str:
    """`recognize_async` 是 WinRT 的异步操作，只能在事件循环里等；调用方在独立线程里跑这个循环。"""

    languages = list(OcrEngine.available_recognizer_languages)
    if not languages:
        raise TextUnreadable("系统里没有可用的 OCR 识别语言")
    writer = DataWriter()
    writer.write_bytes(image.convert("RGBA").tobytes("raw", "BGRA"))
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, image.width, image.height
    )
    readings = []
    for language in languages:
        engine = OcrEngine.try_create_from_language(language)
        if engine is None:
            continue
        try:
            readings.append((await engine.recognize_async(bitmap)).text)
        except OSError as error:
            raise TextUnreadable(str(error)) from error
    if not readings:
        raise TextUnreadable("没能创建任何一种语言的 OCR 引擎")
    return " ".join(readings)


def _print_window(handle: int, width: int, height: int) -> Image.Image:
    """让窗口把自己画进一张位图，不受遮挡影响。

    `PW_RENDERFULLCONTENT` 让 DWM 合成的内容（Chromium、UWP 等）也能画出来，否则只得到黑图。
    """

    window_dc = win32gui.GetWindowDC(handle)
    source = win32ui.CreateDCFromHandle(window_dc)
    target = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source, width, height)
        target.SelectObject(bitmap)
        if not _user32.PrintWindow(handle, target.GetSafeHdc(), _PW_RENDERFULLCONTENT):
            raise win32ui.error("PrintWindow 失败")
        return Image.frombuffer(
            "RGB", (width, height), bitmap.GetBitmapBits(True), "raw", "BGRX", 0, 1
        )
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        target.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(handle, window_dc)


def _extended_frame_bounds(handle: int) -> Rect:
    """窗口可见部分的矩形。

    Windows 10 起 `GetWindowRect` 含一圈约 7 像素的不可见缩放边框，截进来是一圈黑边；
    DWM 的扩展边框矩形去掉了它。取不到时退回窗口矩形。
    """

    rect = wintypes.RECT()
    try:
        _dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(handle),
            wintypes.DWORD(_DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
    except OSError:
        left, top, right, bottom = win32gui.GetWindowRect(handle)
        return Rect(left=left, top=top, width=right - left, height=bottom - top)
    return Rect(
        left=rect.left, top=rect.top, width=rect.right - rect.left, height=rect.bottom - rect.top
    )


def _declare_dpi_awareness() -> None:
    """声明 `PER_MONITOR_AWARE_V2`（ADR-0001）。

    进程内只能设一次，重复设置会失败，因此已经是这个值时直接返回。声明失败必须炸：
    此时窗口矩形会退回逻辑像素，125% 缩放下所有坐标整体偏 0.8 倍，而看起来仍然像一组合理的值。
    """

    context = wintypes.HANDLE(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    if _user32.AreDpiAwarenessContextsEqual(
        _user32.GetThreadDpiAwarenessContext(), context
    ):
        return
    if not _user32.SetProcessDpiAwarenessContext(context):
        raise ctypes.WinError(ctypes.get_last_error())


def _collect(handle: int, windows: list[Window]) -> None:
    """读一个窗口并收进 `windows`；枚举途中关掉的窗口直接丢弃。

    枚举拿到的 HWND 只是那一刻的事实，读它的属性时窗口可能已经不在了。
    这种窗口本就不该出现在观察里，不值得让整次调用失败。
    """

    try:
        windows.append(_read(handle))
    except win32gui.error:
        return


def _read(handle: int) -> Window:
    left, top, right, bottom = win32gui.GetWindowRect(handle)
    process_id = _owning_process(handle)
    return Window(
        handle=handle,
        title=win32gui.GetWindowText(handle),
        process_id=process_id,
        process_name=_process_name(process_id),
        rect=Rect(left=left, top=top, width=right - left, height=bottom - top),
        is_visible=_is_user_facing(handle),
        is_minimized=bool(win32gui.IsIconic(handle)),
        owner=win32gui.GetWindow(handle, win32con.GW_OWNER) or None,
    )


def _owning_process(handle: int) -> int:
    """窗口真正所属的进程。

    UWP 应用（系统设置、计算器等）的顶层窗口是 `ApplicationFrameHost.exe` 画的框，
    应用本身的界面是框里另一个进程的子窗口；此时取那个子窗口的进程，否则系统设置认不出来。
    应用挂起时框里没有这个子窗口，只好退回框的进程。
    """

    _, frame_process = win32process.GetWindowThreadProcessId(handle)
    if win32gui.GetClassName(handle) != "ApplicationFrameWindow":
        return int(frame_process)
    children: list[int] = []
    win32gui.EnumChildWindows(handle, lambda child, _: children.append(child), None)
    for child in children:
        _, process = win32process.GetWindowThreadProcessId(child)
        if process != frame_process:
            return int(process)
    return int(frame_process)


def _ancestry(process_id: int) -> frozenset[int]:
    """`process_id` 及其各级父进程。

    只在启动时取一次：中间某一级日后退出了，链条也不会因此断开、把 Agent 的窗口漏掉。
    某一级在本服务启动前就已退出时，更上面的祖先无从得知。
    """

    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    parents: dict[int, int] = {}
    try:
        entry = _PROCESSENTRY32W(dwSize=ctypes.sizeof(_PROCESSENTRY32W))
        more = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            more = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    chain = [process_id]
    while (parent := parents.get(chain[-1])) and parent not in chain:
        chain.append(parent)
    return frozenset(chain)


def _is_user_facing(handle: int) -> bool:
    """窗口是否是用户看得见、切得到的那一类。

    判据取 alt-tab 的口径：未被隐藏或 cloaked，不是工具窗口，不是别人的附属窗口，
    也不是桌面本身。进程留在后台的消息窗口与 1×1 的辅助窗口都由此排除。
    """

    if not win32gui.IsWindowVisible(handle) or _is_cloaked(handle):
        return False
    if handle == _user32.GetShellWindow():
        return False
    extended_style = win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE)
    if extended_style & win32con.WS_EX_TOOLWINDOW:
        return False
    owner = win32gui.GetWindow(handle, win32con.GW_OWNER)
    return not owner or bool(extended_style & win32con.WS_EX_APPWINDOW)


def _is_cloaked(handle: int) -> bool:
    """窗口是否被 DWM 隐藏。

    UWP 的 `ApplicationFrameWindow` 会为未启动的应用留下带标题的空壳，
    这些壳 `IsWindowVisible` 为真，只有 cloaked 属性能把它们认出来。
    """

    cloaked = wintypes.DWORD(0)
    try:
        _dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(handle),
            wintypes.DWORD(_DWMWA_CLOAKED),
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
    except OSError:
        return False
    return cloaked.value != 0


def _process_name(process_id: int) -> str:
    """进程的可执行文件名；够不到那个进程时返回空串。

    提权进程的句柄在普通权限下一律打不开，这是预期情形而非错误：窗口本身仍要列出。
    """

    process = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not process:
        return ""
    try:
        path = ctypes.create_unicode_buffer(_EXTENDED_MAX_PATH)
        length = wintypes.DWORD(_EXTENDED_MAX_PATH)
        if not _kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(length)):
            return ""
        return path.value.rsplit("\\", 1)[-1]
    finally:
        _kernel32.CloseHandle(process)


_ALLOW = 1
_DENY = 2
_CONFIRM_CLASS = "ComputerUseConfirm"
_confirm_wndproc: Callable[[int, int, int, int], int] | None = None
_translate_message = cast(Callable[[object], None], win32gui.TranslateMessage)
_dispatch_message = cast(Callable[[object], None], win32gui.DispatchMessage)


class _DialogOutcome:
    """一次原生确认还没结束时的答复。超时把 `reply` 留成 `None`。"""

    def __init__(self) -> None:
        self.reply: bool | None = None
        self.done = False


_confirm_state: dict[int, _DialogOutcome] = {}


def _native_confirm(title: str, message: str, image: Image.Image | None, timeout: float) -> bool | None:
    """顶层系统窗口：允许 / 拒绝，到时无人理则按超时。调用期间模型还停在这次工具调用里。"""

    hinst = _kernel32.GetModuleHandleW(None)
    _ensure_confirm_class(hinst)
    bitmap, image_size = _confirm_bitmap(image)
    width = 520
    text_height = 160
    image_height = image_size[1] if image_size is not None else 0
    gap = 12 if image_height else 0
    button_top = 16 + text_height + gap + image_height + 12
    height = button_top + 32 + 16 + 40
    screen_w = _user32.GetSystemMetrics(win32con.SM_CXSCREEN)
    screen_h = _user32.GetSystemMetrics(win32con.SM_CYSCREEN)
    hwnd = win32gui.CreateWindowEx(
        win32con.WS_EX_TOPMOST | win32con.WS_EX_DLGMODALFRAME,
        _CONFIRM_CLASS,
        title,
        win32con.WS_POPUP | win32con.WS_CAPTION | win32con.WS_SYSMENU | win32con.WS_VISIBLE,
        max(0, (screen_w - width) // 2),
        max(0, (screen_h - height) // 2),
        width,
        height,
        0,
        0,
        hinst,
        None,
    )
    state = _DialogOutcome()
    _confirm_state[hwnd] = state
    edit = win32gui.CreateWindow(
        "EDIT",
        message,
        win32con.WS_CHILD
        | win32con.WS_VISIBLE
        | win32con.WS_VSCROLL
        | win32con.ES_MULTILINE
        | win32con.ES_READONLY
        | win32con.ES_AUTOVSCROLL,
        16,
        16,
        width - 32,
        text_height,
        hwnd,
        0,
        hinst,
        None,
    )
    win32gui.SendMessage(edit, win32con.EM_SETSEL, 0, 0)
    if bitmap is not None and image_size is not None:
        static = win32gui.CreateWindow(
            "STATIC",
            "",
            win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.SS_BITMAP,
            16,
            16 + text_height + gap,
            image_size[0],
            image_size[1],
            hwnd,
            0,
            hinst,
            None,
        )
        win32gui.SendMessage(static, win32con.STM_SETIMAGE, win32con.IMAGE_BITMAP, bitmap)
    deny = win32gui.CreateWindow(
        "BUTTON",
        "拒绝",
        win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_TABSTOP | win32con.BS_DEFPUSHBUTTON,
        width - 16 - 80 - 12 - 80,
        button_top,
        80,
        28,
        hwnd,
        _DENY,
        hinst,
        None,
    )
    win32gui.CreateWindow(
        "BUTTON",
        "允许",
        win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_TABSTOP,
        width - 16 - 80,
        button_top,
        80,
        28,
        hwnd,
        _ALLOW,
        hinst,
        None,
    )
    win32gui.SetFocus(deny)
    win32gui.SetWindowPos(
        hwnd,
        win32con.HWND_TOPMOST,
        0,
        0,
        0,
        0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
    )
    win32gui.SetForegroundWindow(hwnd)
    _user32.SetTimer(hwnd, 1, max(1, int(timeout * 1000)), None)
    try:
        while not state.done:
            code, msg = win32gui.GetMessage(0, 0, 0)
            if code == 0 or code == -1:
                break
            _translate_message(msg)
            _dispatch_message(msg)
    finally:
        _confirm_state.pop(hwnd, None)
        if bitmap is not None:
            win32gui.DeleteObject(bitmap)
    return state.reply


def _ensure_confirm_class(hinst: int) -> None:
    global _confirm_wndproc
    if _confirm_wndproc is not None:
        return
    window_class: Any = win32gui.WNDCLASS()
    window_class.hInstance = hinst
    window_class.lpszClassName = _CONFIRM_CLASS
    _confirm_wndproc = _confirm_window_proc
    window_class.lpfnWndProc = _confirm_wndproc
    window_class.hbrBackground = win32con.COLOR_WINDOW + 1
    window_class.hCursor = _user32.LoadCursorW(None, ctypes.cast(32512, wintypes.LPCWSTR))
    try:
        win32gui.RegisterClass(window_class)
    except win32gui.error:
        pass


def _confirm_window_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    state = _confirm_state.get(hwnd)
    if msg == win32con.WM_COMMAND and state is not None:
        command = wparam & 0xFFFF
        if command == _ALLOW:
            state.reply = True
            win32gui.DestroyWindow(hwnd)
            return 0
        if command == _DENY:
            state.reply = False
            win32gui.DestroyWindow(hwnd)
            return 0
    if msg == win32con.WM_TIMER and state is not None:
        state.reply = None
        win32gui.DestroyWindow(hwnd)
        return 0
    if msg == win32con.WM_CLOSE and state is not None:
        state.reply = False
        win32gui.DestroyWindow(hwnd)
        return 0
    if msg == win32con.WM_DESTROY and state is not None:
        state.done = True
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def _confirm_bitmap(image: Image.Image | None) -> tuple[Any, tuple[int, int]] | tuple[None, None]:
    if image is None:
        return None, None
    fitted = image.convert("RGB")
    fitted.thumbnail((480, 280))
    fd, name = tempfile.mkstemp(suffix=".bmp")
    os.close(fd)
    try:
        fitted.save(name, format="BMP")
        handle: Any = win32gui.LoadImage(
            0, name, win32con.IMAGE_BITMAP, 0, 0, win32con.LR_LOADFROMFILE
        )
    except (OSError, win32gui.error):
        return None, None
    finally:
        Path(name).unlink(missing_ok=True)
    if not handle:
        return None, None
    return handle, fitted.size
