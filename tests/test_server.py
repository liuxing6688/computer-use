"""MCP 工具壳：工具确实被注册，且观察结果能原样穿过 MCP 层。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Client

from computer_use.desktop import Rect
from computer_use.server import create_server

from .fake_desktop import FakeDesktop, window


def test_列出窗口可经由_MCP_调用() -> None:
    desktop = FakeDesktop(
        [
            window(
                handle=0x1234,
                title="无标题 - 记事本",
                process_name="notepad.exe",
                rect=Rect(left=100, top=50, width=800, height=600),
            ),
            window(handle=0x5678, title="缩到任务栏的窗口", is_minimized=True),
        ]
    )

    async def call() -> Any:
        async with Client(create_server(desktop)) as client:
            return await client.call_tool("list_windows", {})

    result = asyncio.run(call())

    assert result.data == [
        {
            "handle": 0x1234,
            "title": "无标题 - 记事本",
            "process_name": "notepad.exe",
            "rect": {"left": 100, "top": 50, "width": 800, "height": 600},
        }
    ]
