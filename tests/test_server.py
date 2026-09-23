"""MCP 工具壳：工具确实被注册，且观察结果能原样穿过 MCP 层。"""

from __future__ import annotations

import asyncio
import base64
import io
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError
from mcp.types import ImageContent
from PIL import Image

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


def test_观察与放大可经由_MCP_调用_截图与元数据一并返回() -> None:
    desktop = FakeDesktop(
        [window(handle=0x1234, rect=Rect(left=100, top=50, width=320, height=240))],
        images={0x1234: Image.new("RGB", (320, 240), "red")},
    )

    async def call() -> tuple[CallToolResult, CallToolResult]:
        async with Client(create_server(desktop)) as client:
            observed = await client.call_tool("observe_window", {"handle": 0x1234})
            zoomed = await client.call_tool(
                "zoom",
                {
                    "screenshot_id": observed.data["screenshot_id"],
                    "left": 10,
                    "top": 20,
                    "width": 30,
                    "height": 40,
                },
            )
            return observed, zoomed

    observed, zoomed = asyncio.run(call())

    assert _image_size(observed) == (320, 240)
    assert observed.data["window"]["handle"] == 0x1234
    assert observed.data["screen_offset"] == {"x": 100, "y": 50}
    assert _image_size(zoomed) == (30, 40)
    assert zoomed.data["window_offset"] == {"x": 10, "y": 20}


def test_观察不可操作的窗口经由_MCP_返回错误() -> None:
    async def call() -> None:
        async with Client(create_server(FakeDesktop())) as client:
            await client.call_tool("observe_window", {"handle": 404})

    with pytest.raises(ToolError, match="404"):
        asyncio.run(call())


def _image_size(result: CallToolResult) -> tuple[int, int]:
    [image] = [c for c in result.content if isinstance(c, ImageContent)]
    assert image.mime_type == "image/png"
    size: tuple[int, int] = Image.open(io.BytesIO(base64.b64decode(image.data))).size
    return size
