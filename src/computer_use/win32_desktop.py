"""`DesktopPort` 的 Windows 实现。除本模块外，仓库任何地方都不应 import Win32 库。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Sequence

import win32con
import win32gui
import win32process

from computer_use.desktop import Rect, Window

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DWMWA_CLOAKED = 14
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

    构造即声明 `PER_MONITOR_AWARE_V2`，因此窗口矩形一律是物理像素。
    """

    def __init__(self) -> None:
        _declare_dpi_awareness()

    def list_windows(self) -> Sequence[Window]:
        windows: list[Window] = []
        win32gui.EnumWindows(lambda handle, _: _collect(handle, windows), None)
        return tuple(windows)


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
    return Window(
        handle=handle,
        title=win32gui.GetWindowText(handle),
        process_name=_process_name(handle),
        rect=Rect(left=left, top=top, width=right - left, height=bottom - top),
        is_visible=_is_user_facing(handle),
        is_minimized=bool(win32gui.IsIconic(handle)),
    )


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


def _process_name(handle: int) -> str:
    """窗口所属进程的可执行文件名；够不到那个进程时返回空串。

    提权进程的句柄在普通权限下一律打不开，这是预期情形而非错误：窗口本身仍要列出。
    """

    _, pid = win32process.GetWindowThreadProcessId(handle)
    process = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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
