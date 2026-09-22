"""真实桌面验收：以记事本为替身，证明平台边界与 stdio 传输在真机上成立。

默认不跑（见 `pyproject.toml` 的 `addopts`），需要一个真实的桌面会话：
`uv run pytest -m realdesktop`。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from typing import Any, Callable, Iterator

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from computer_use.tools import list_windows
from computer_use.win32_desktop import Win32Desktop

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


def test_server_能被_stdio_客户端连上并调用(notepad: subprocess.Popen[bytes]) -> None:
    """Claude Code 就是这样连上来的：拉起一个子进程，走 stdio 说 MCP。"""

    transport = StdioTransport(
        command=sys.executable, args=["-m", "computer_use.server"]
    )

    async def call() -> list[dict[str, Any]]:
        async with Client(transport) as client:
            assert [tool.name for tool in await client.list_tools()] == ["list_windows"]
            result = await client.call_tool("list_windows", {})
            return list(result.data)

    window = _await_notepad(lambda: asyncio.run(call()))

    assert "记事本" in window["title"] or "Notepad" in window["title"]


def _await_notepad(
    observe: Callable[[], list[dict[str, Any]]], timeout: float = 15.0
) -> dict[str, Any]:
    """等到记事本的窗口出现在观察里；窗口创建相对进程启动有延迟。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in observe():
            if window["process_name"].lower() == "notepad.exe":
                return window
        time.sleep(0.2)
    raise AssertionError(f"记事本的窗口在 {timeout}s 内没有出现在观察里")
