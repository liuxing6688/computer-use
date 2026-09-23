"""`DesktopPort` 的 Windows 实现。除本模块外，仓库任何地方都不应 import Win32 库。"""

from __future__ import annotations

import asyncio
import ctypes
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Collection, Sequence

import win32con
import win32gui
import win32process
import win32ui
from PIL import Image
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter

from computer_use.desktop import Capture, Rect, TextUnreadable, Window, WindowUnavailable

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_PW_RENDERFULLCONTENT = 0x2
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_EXTENDED_MAX_PATH = 32768

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


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _INPUT(ctypes.Structure):
    """`INPUT` 只取鼠标一支；`MOUSEINPUT` 是联合体里最大的成员，结构体大小不变。"""

    _fields_ = (("type", wintypes.DWORD), ("mi", _MOUSEINPUT))


_user32.SendInput.restype = wintypes.UINT
_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)


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
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
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

        先 `SetCursorPos` 再注入不带坐标的按键：`SendInput` 的绝对坐标要归一化到 0–65535，
        取整会让落点偏一个像素，而 `SetCursorPos` 是精确的。
        鼠标输入送往光标下的窗口并顺带激活它，不必先 `SetForegroundWindow`；键盘输入才须如此。
        """

        if not _user32.SetCursorPos(x, y):
            raise ctypes.WinError(ctypes.get_last_error())
        cursor = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(cursor))
        if (cursor.x, cursor.y) != (x, y):
            raise OSError(f"光标没能移到 ({x}, {y})，停在了 ({cursor.x}, {cursor.y})")
        inputs = (_INPUT * 2)(
            _INPUT(type=_INPUT_MOUSE, mi=_MOUSEINPUT(dwFlags=_MOUSEEVENTF_LEFTDOWN)),
            _INPUT(type=_INPUT_MOUSE, mi=_MOUSEINPUT(dwFlags=_MOUSEEVENTF_LEFTUP)),
        )
        if _user32.SendInput(len(inputs), inputs, ctypes.sizeof(_INPUT)) != len(inputs):
            raise ctypes.WinError(ctypes.get_last_error())

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
