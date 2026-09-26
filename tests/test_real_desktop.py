"""真实桌面验收：以记事本为替身，证明平台边界与 stdio 传输在真机上成立。

默认不跑（见 `pyproject.toml` 的 `addopts`），需要一个真实的桌面会话：
`uv run pytest -m realdesktop`。
"""

from __future__ import annotations

import asyncio
import ctypes
import io
import json
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Iterator, cast

import pytest
import win32clipboard
import win32con
import win32gui
import win32process
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from PIL import Image

from computer_use.desktop import Rect
from computer_use.observation import Screenshots
from computer_use.server import create_server
from computer_use.tools import list_windows, observe_window
from computer_use.win32_desktop import Win32Desktop

from .support import assert_no_retry_instruction

pytestmark = pytest.mark.realdesktop


@pytest.fixture
def notepad() -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(["notepad.exe"])
    try:
        yield process
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_列出窗口能在真实桌面上找到记事本(notepad: subprocess.Popen[bytes]) -> None:
    desktop = Win32Desktop()

    window = _await_notepad(lambda: list_windows(desktop))

    assert "记事本" in window["title"] or "Notepad" in window["title"]
    assert window["rect"]["width"] >= 100 and window["rect"]["height"] >= 100


def test_观察能截下记事本_几何为物理像素(notepad: subprocess.Popen[bytes]) -> None:
    desktop = Win32Desktop()
    window = _await_notepad(lambda: list_windows(desktop))

    observed = observe_window(desktop, Screenshots(), handle=window["handle"])

    image = Image.open(io.BytesIO(observed.png)).convert("RGB")
    metadata = observed.metadata
    assert image.size == (metadata["size"]["width"], metadata["size"]["height"])
    assert image.getextrema() != ((0, 0),) * 3, "截图是一张黑图，PrintWindow 没画出窗口内容"
    region = Rect(
        left=metadata["screen_offset"]["x"],
        top=metadata["screen_offset"]["y"],
        width=round(image.width / metadata["scale"]),
        height=round(image.height / metadata["scale"]),
    )
    listed = window["rect"]
    assert listed["left"] <= region.left
    assert listed["top"] <= region.top
    assert region.left + region.width <= listed["left"] + listed["width"]
    assert region.top + region.height <= listed["top"] + listed["height"]
    dpi_scale, logical_width = _dpi_unaware_view(window["handle"])
    assert metadata["dpi_scale"] == dpi_scale
    assert abs(listed["width"] - logical_width * dpi_scale) <= 2


def test_失败的调用在真实磁盘上留下日志与记事本的截图(
    notepad: subprocess.Popen[bytes], tmp_path: Path
) -> None:
    desktop = Win32Desktop(data_dir=tmp_path)
    window = _await_notepad(lambda: list_windows(desktop))

    async def call() -> None:
        async with Client(create_server(desktop)) as client:
            observed = await client.call_tool("observe_window", {"handle": window["handle"]})
            with pytest.raises(ToolError):
                await client.call_tool(
                    "zoom",
                    {
                        "screenshot_id": observed.data["metadata"]["screenshot_id"],
                        "left": -1,
                        "top": 0,
                        "width": 10,
                        "height": 10,
                    },
                )

    asyncio.run(call())

    lines = (tmp_path / "actions.jsonl").read_text(encoding="utf-8").splitlines()
    observed, zoomed = (json.loads(line) for line in lines)
    assert (observed["tool"], observed["outcome"], observed["evidence"]) == (
        "observe_window",
        "succeeded",
        None,
    )
    assert (zoomed["tool"], zoomed["outcome"]) == ("zoom", "failed")
    evidence = Path(zoomed["evidence"])
    assert evidence.parent == tmp_path / "evidence"
    assert Image.open(evidence).width >= 100
    assert list((tmp_path / "evidence").iterdir()) == [evidence]


def test_点击按截图像素坐标落在记事本上_缩放下落点一致(
    notepad: subprocess.Popen[bytes], tmp_path: Path
) -> None:
    desktop = Win32Desktop(data_dir=tmp_path)
    window = _await_notepad(lambda: list_windows(desktop))

    async def call() -> tuple[dict[str, Any], dict[str, Any]]:
        async with Client(create_server(desktop)) as client:
            await client.call_tool("declare_scope", {"handles": [window["handle"]]})
            observed = await client.call_tool("observe_window", {"handle": window["handle"]})
            size = observed.data["metadata"]["size"]
            clicked = await client.call_tool(
                "click",
                {
                    "screenshot_id": observed.data["metadata"]["screenshot_id"],
                    "x": size["width"] // 2,
                    "y": size["height"] // 2,
                    "intent": "点记事本的编辑区",
                    "dangerous": False,
                },
            )
            return observed.data, clicked.data

    observed, clicked = asyncio.run(call())

    point, scale = clicked["screen_point"], observed["scale"]
    center_x = observed["screen_offset"]["x"] + observed["size"]["width"] / scale / 2
    center_y = observed["screen_offset"]["y"] + observed["size"]["height"] / scale / 2
    assert abs(point["x"] - center_x) <= 1 / scale
    assert abs(point["y"] - center_y) <= 1 / scale
    assert clicked["window"]["handle"] == window["handle"]
    assert win32gui.GetForegroundWindow() == window["handle"], "点击没有落在记事本上"
    logical_x, logical_y = _dpi_unaware_cursor()
    assert abs(logical_x * observed["dpi_scale"] - point["x"]) <= observed["dpi_scale"]
    assert abs(logical_y * observed["dpi_scale"] - point["y"]) <= observed["dpi_scale"]
    records = [
        json.loads(line)
        for line in (tmp_path / "actions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(r["tool"], r["outcome"]) for r in records] == [
        ("declare_scope", "succeeded"),
        ("observe_window", "succeeded"),
        ("click", "succeeded"),
    ]


_WORD_WINDOW = """
import ctypes, sys, threading, win32gui, win32con
from ctypes import wintypes
ctypes.windll.user32.SetProcessDpiAwarenessContext(wintypes.HANDLE(-4))
hinst = win32gui.GetModuleHandle(None)
hwnd = win32gui.CreateWindow(
    "STATIC", "高危词样本",
    win32con.WS_POPUP | win32con.WS_VISIBLE | win32con.WS_CAPTION,
    200, 200, 420, 220, 0, 0, hinst, None,
)
win32gui.CreateWindow(
    "STATIC", "    删除    ",
    win32con.WS_CHILD | win32con.WS_VISIBLE,
    30, 50, 320, 80, hwnd, 0, hinst, None,
)
win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
win32gui.UpdateWindow(hwnd)
sys.stdout.write(str(hwnd) + "\\n")
sys.stdout.flush()

def wait() -> None:
    sys.stdin.read()
    win32gui.PostMessage(hwnd, win32con.WM_QUIT, 0, 0)

threading.Thread(target=wait, daemon=True).start()
win32gui.PumpMessages()
"""


def test_点在写着高危词的地方_真实_OCR_在同一次调用里问人_确认后才点击(tmp_path: Path) -> None:
    # 新版记事本的编辑框不会把 SetWindowText 画进截图，OCR 读不到。另开一个进程画这几个字。
    proc = subprocess.Popen(
        [sys.executable, "-c", _WORD_WINDOW],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert proc.stdout is not None and proc.stdin is not None
    try:
        hwnd = int(proc.stdout.readline())
        desktop = Win32Desktop(data_dir=tmp_path / "computer-use")
        _await_handle(lambda: list_windows(desktop), hwnd)
        label = win32gui.FindWindowEx(hwnd, 0, "STATIC", None)
        assert label, "没找到写着高危词的控件"
        left, top, _, _ = win32gui.GetWindowRect(label)
        asked: dict[str, str] = {}
        failed: list[BaseException] = []

        def allow() -> None:
            try:
                asked["text"] = _answer_native_confirm("允许")
            except BaseException as error:
                failed.append(error)

        async def call() -> dict[str, Any]:
            async with Client(create_server(desktop)) as client:
                await client.call_tool("declare_scope", {"handles": [hwnd]})
                observed = await client.call_tool("observe_window", {"handle": hwnd})
                meta = observed.data["metadata"]
                request = {
                    "screenshot_id": meta["screenshot_id"],
                    "x": round((left + 40 * meta["dpi_scale"] - meta["screen_offset"]["x"]) * meta["scale"]),
                    "y": round((top + 20 * meta["dpi_scale"] - meta["screen_offset"]["y"]) * meta["scale"]),
                    "intent": "点这个字",
                    "dangerous": False,
                }
                confirmer = threading.Thread(target=allow)
                confirmer.start()
                try:
                    clicked = await client.call_tool("click", request)
                finally:
                    confirmer.join(timeout=20)
                return cast(dict[str, Any], clicked.data)

        clicked = asyncio.run(call())
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)

    if failed:
        raise failed[0]
    assert "删除" in asked["text"]
    assert_no_retry_instruction(asked["text"])
    assert clicked["window"]["handle"] == hwnd
    tickets = tmp_path / "computer-use" / "tickets"
    assert not tickets.exists() or list(tickets.iterdir()) == []


def test_中文打进记事本_原来的剪贴板内容被放回(
    notepad: subprocess.Popen[bytes], tmp_path: Path
) -> None:
    desktop = Win32Desktop(data_dir=tmp_path)
    saved = desktop.read_clipboard()
    marker = "用户原来复制的"
    custom = win32clipboard.RegisterClipboardFormat("computer-use-type-text-test")
    try:
        _put_clipboard(marker, custom, b"opaque")
        window = _await_notepad(lambda: list_windows(desktop), pid=notepad.pid)

        async def call() -> dict[str, Any]:
            async with Client(create_server(desktop)) as client:
                await client.call_tool("declare_scope", {"handles": [window["handle"]]})
                observed = await client.call_tool("observe_window", {"handle": window["handle"]})
                typed = await client.call_tool(
                    "type_text",
                    {
                        "screenshot_id": observed.data["metadata"]["screenshot_id"],
                        "text": "你好",
                        "intent": "在记事本里输入中文",
                        "dangerous": False,
                    },
                )
                return dict(typed.data)

        result = asyncio.run(call())
        time.sleep(0.2)

        assert _edit_text(window["handle"]) == "你好"
        assert result["tier"] == "clipboard"
        assert result["clipboard_used"] is True
        assert result["window"]["handle"] == window["handle"]
        assert _clipboard_unicode() == marker
        assert _clipboard_bytes(custom) == b"opaque"
        assert win32gui.GetForegroundWindow() == window["handle"]
    finally:
        desktop.restore_clipboard(saved)


def test_逐字符注入能把中文打进记事本(notepad: subprocess.Popen[bytes]) -> None:
    desktop = Win32Desktop()
    window = _await_notepad(lambda: list_windows(desktop), pid=notepad.pid)
    desktop.focus(window["handle"])

    for character in "你好":
        desktop.type_character(character)
    time.sleep(0.3)

    assert _edit_text(window["handle"]) == "你好"
    assert win32gui.GetForegroundWindow() == window["handle"]


def test_启动记事本等到窗口_按键落进它(tmp_path: Path) -> None:
    desktop = Win32Desktop(data_dir=tmp_path)
    pids: set[int] = set()
    try:

        async def call() -> dict[str, Any]:
            async with Client(create_server(desktop)) as client:
                launched = await client.call_tool(
                    "launch_app",
                    {"app": "notepad", "intent": "打开记事本", "dangerous": False},
                )
                window = launched.data["window"]
                await client.call_tool("declare_scope", {"handles": [window["handle"]]})
                observed = await client.call_tool("observe_window", {"handle": window["handle"]})
                await client.call_tool(
                    "press_keys",
                    {
                        "screenshot_id": observed.data["metadata"]["screenshot_id"],
                        "keys": ["ctrl", "o"],
                        "intent": "打开文件对话框",
                        "dangerous": False,
                    },
                )
                return dict(launched.data)

        launched = asyncio.run(call())
        handle = int(launched["window"]["handle"])
        pids.add(int(launched["process_id"]))
        pids.add(_process_id(handle))

        assert launched["window"]["process_name"].lower() == "notepad.exe"
        assert _await_dialog(desktop, handle), "Ctrl+O 没有在记事本里打开文件对话框"
    finally:
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


def test_记事本端到端_找到窗口_观察_定位_点击_输入_保存_全程留下日志(tmp_path: Path) -> None:
    """打开一份已有文件，走完整条链路后 Ctrl+S 就地存盘。

    本机记事本的「另存为」是带所有者的对话框，不出现在可操作窗口里；
    对已经关联了路径的文档按保存，存盘不依赖那扇对话框。
    """

    saved = tmp_path / "acceptance.txt"
    saved.write_text("", encoding="utf-8")
    desktop = Win32Desktop(data_dir=tmp_path / "computer-use")
    process = subprocess.Popen(["notepad.exe", str(saved)])
    try:
        window = _await_notepad(lambda: list_windows(desktop), pid=process.pid)

        async def call() -> None:
            async with Client(create_server(desktop)) as client:
                found = await client.call_tool("list_windows", {})
                assert any(item["handle"] == window["handle"] for item in found.data)
                await client.call_tool("declare_scope", {"handles": [window["handle"]]})
                observed = await client.call_tool("observe_window", {"handle": window["handle"]})
                size = observed.data["metadata"]["size"]
                zoomed = await client.call_tool(
                    "zoom",
                    {
                        "screenshot_id": observed.data["metadata"]["screenshot_id"],
                        "left": size["width"] // 4,
                        "top": size["height"] // 4,
                        "width": size["width"] // 2,
                        "height": size["height"] // 2,
                    },
                )
                zoom_size = zoomed.data["metadata"]["size"]
                clicked = await client.call_tool(
                    "click",
                    {
                        "screenshot_id": zoomed.data["metadata"]["screenshot_id"],
                        "x": zoom_size["width"] // 2,
                        "y": zoom_size["height"] // 2,
                        "intent": "点记事本的编辑区",
                        "dangerous": False,
                    },
                )
                assert clicked.data["window"]["handle"] == window["handle"]
                point = clicked.data["screen_point"]
                rect = window["rect"]
                assert rect["left"] <= point["x"] <= rect["left"] + rect["width"]
                assert rect["top"] <= point["y"] <= rect["top"] + rect["height"]
                await client.call_tool(
                    "type_text",
                    {
                        "screenshot_id": observed.data["metadata"]["screenshot_id"],
                        "text": "computer-use-acceptance",
                        "intent": "在记事本里输入验收文本",
                        "dangerous": False,
                    },
                )
                await client.call_tool(
                    "press_keys",
                    {
                        "screenshot_id": observed.data["metadata"]["screenshot_id"],
                        "keys": ["ctrl", "s"],
                        "intent": "保存",
                        "dangerous": False,
                    },
                )

        asyncio.run(call())
        deadline = time.monotonic() + 5
        text = ""
        while time.monotonic() < deadline:
            text = saved.read_text(encoding="utf-8")
            if "computer-use-acceptance" in text:
                break
            time.sleep(0.2)
        assert "computer-use-acceptance" in text
    finally:
        process.terminate()
        process.wait(timeout=10)

    records = [
        json.loads(line)
        for line in (tmp_path / "computer-use" / "actions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["tool"] for record in records] == [
        "list_windows",
        "declare_scope",
        "observe_window",
        "zoom",
        "click",
        "type_text",
        "press_keys",
    ]
    assert all(record["outcome"] == "succeeded" for record in records)


def test_server_能被_stdio_客户端连上并调用(notepad: subprocess.Popen[bytes]) -> None:
    """Claude Code 就是这样连上来的：拉起一个子进程，走 stdio 说 MCP。"""

    transport = StdioTransport(
        command=sys.executable, args=["-m", "computer_use.server"]
    )

    async def call() -> list[dict[str, Any]]:
        async with Client(transport) as client:
            assert sorted(tool.name for tool in await client.list_tools()) == [
                "click",
                "declare_scope",
                "double_click",
                "drag",
                "get_scope",
                "launch_app",
                "list_windows",
                "observe_window",
                "press_keys",
                "resume",
                "right_click",
                "scroll",
                "type_text",
                "zoom",
            ]
            result = await client.call_tool("list_windows", {})
            return list(result.data)

    window = _await_notepad(lambda: asyncio.run(call()))

    assert "记事本" in window["title"] or "Notepad" in window["title"]


def _edit_text(handle: int) -> str:
    """读记事本编辑框里的文字。

    `GetWindowText` 对别的进程的控件只返回标题，读不到编辑框里刚敲进去的内容。
    """

    edit = win32gui.FindWindowEx(handle, 0, "Edit", None)
    assert edit, "没找到记事本的编辑框"
    send = ctypes.WinDLL("user32", use_last_error=True).SendMessageW
    send.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    send.restype = ctypes.c_ssize_t
    length = send(edit, win32con.WM_GETTEXTLENGTH, 0, 0)
    buffer = ctypes.create_unicode_buffer(length + 1)
    send(edit, win32con.WM_GETTEXT, length + 1, ctypes.addressof(buffer))
    return buffer.value


def _put_clipboard(text: str, custom_format: int, custom: bytes) -> None:
    win32clipboard.OpenClipboard(None)
    try:
        win32clipboard.EmptyClipboard()  # type: ignore[no-untyped-call]
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)  # type: ignore[no-untyped-call]
        win32clipboard.SetClipboardData(custom_format, custom)  # type: ignore[no-untyped-call]
    finally:
        win32clipboard.CloseClipboard()  # type: ignore[no-untyped-call]


def _clipboard_unicode() -> str | None:
    win32clipboard.OpenClipboard(None)
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return None
        data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()  # type: ignore[no-untyped-call]
    return data if isinstance(data, str) else None


def _clipboard_bytes(fmt: int) -> bytes | None:
    win32clipboard.OpenClipboard(None)
    try:
        if not win32clipboard.IsClipboardFormatAvailable(fmt):
            return None
        data = win32clipboard.GetClipboardData(fmt)
    finally:
        win32clipboard.CloseClipboard()  # type: ignore[no-untyped-call]
    return data if isinstance(data, bytes) else None


def _dpi_unaware_view(handle: int) -> tuple[float, int]:
    """一个未声明 DPI 感知的子进程眼里的主显示器缩放比与窗口宽度，与本进程的声明无关。

    未声明感知的进程看到的是逻辑分辨率与逻辑矩形，显示模式给出的是物理分辨率。
    """

    probe = (
        "import sys, win32api, win32gui;"
        "l, _, r, _ = win32gui.GetWindowRect(int(sys.argv[1]));"
        "print(win32api.EnumDisplaySettings(None, -1).PelsWidth / win32api.GetSystemMetrics(0), r - l)"
    )
    output = subprocess.run(
        [sys.executable, "-c", probe, str(handle)], capture_output=True, text=True, check=True
    ).stdout
    scale, width = output.split()
    return round(float(scale), 2), int(width)


def _dpi_unaware_cursor() -> tuple[int, int]:
    """一个未声明 DPI 感知的子进程眼里的光标位置，即逻辑像素，与本进程的声明无关。"""

    output = subprocess.run(
        [sys.executable, "-c", "import win32api; print(*win32api.GetCursorPos())"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    x, y = output.split()
    return int(x), int(y)


def _answer_native_confirm(button: str, timeout: float = 15.0) -> str:
    """点掉本进程弹出的原生确认，返回说明文字。对话框没出现时抛 `AssertionError`。"""

    deadline = time.monotonic() + timeout
    hwnd = 0
    target = 0
    while time.monotonic() < deadline:
        hwnd = win32gui.FindWindow("ComputerUseConfirm", None)
        target = _child_button(hwnd, button) if hwnd else 0
        if target:
            break
        time.sleep(0.05)
    assert hwnd and target, "没有弹出原生确认对话框"
    texts: list[str] = []

    def collect(child: int, _: object) -> bool:
        if win32gui.GetClassName(child) == "Edit":
            texts.append(win32gui.GetWindowText(child))
        return True

    win32gui.EnumChildWindows(hwnd, collect, None)
    win32gui.SendMessage(target, win32con.BM_CLICK, 0, 0)
    return "\n".join(texts)


def _child_button(hwnd: int, label: str) -> int:
    found = 0

    def match(child: int, _: object) -> bool:
        nonlocal found
        if win32gui.GetClassName(child) == "Button" and win32gui.GetWindowText(child) == label:
            found = child
        return True

    win32gui.EnumChildWindows(hwnd, match, None)
    return found


def _await_dialog(desktop: Win32Desktop, owner: int, timeout: float = 5.0) -> bool:
    """等到 `owner` 弹出一个有标题的窗口，例如记事本的打开文件对话框。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in desktop.list_windows():
            if window.owner == owner and window.title.casefold() in {"open", "打开"}:
                return True
        time.sleep(0.2)
    return False


def _await_handle(
    observe: Callable[[], list[dict[str, Any]]], handle: int, timeout: float = 15.0
) -> None:
    """等到这扇窗口出现在观察里。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(window["handle"] == handle for window in observe()):
            return
        time.sleep(0.2)
    raise AssertionError(f"窗口 {handle} 在 {timeout}s 内没有出现在观察里")


def _await_notepad(
    observe: Callable[[], list[dict[str, Any]]],
    timeout: float = 15.0,
    pid: int | None = None,
) -> dict[str, Any]:
    """等到记事本的窗口出现在观察里；窗口创建相对进程启动有延迟。

    `pid` 给出时只认这个进程的窗口，免得桌面上还留着别的记事本。
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in observe():
            if window["process_name"].lower() != "notepad.exe":
                continue
            if pid is not None and _process_id(window["handle"]) != pid:
                continue
            return window
        time.sleep(0.2)
    raise AssertionError(f"记事本的窗口在 {timeout}s 内没有出现在观察里")


def _process_id(handle: int) -> int:
    _, process_id = win32process.GetWindowThreadProcessId(handle)
    return process_id
