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
from computer_use.hook import decide
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


def test_每次工具调用都记入动作日志_失败的调用留下目标窗口的截图() -> None:
    desktop = FakeDesktop(
        [window(handle=0x1234, rect=Rect(left=100, top=50, width=320, height=240))],
        images={0x1234: Image.new("RGB", (320, 240), "red")},
    )

    async def call() -> str:
        async with Client(create_server(desktop)) as client:
            await client.call_tool("list_windows", {})
            observed = await client.call_tool("observe_window", {"handle": 0x1234})
            screenshot_id: str = observed.data["screenshot_id"]
            with pytest.raises(ToolError):
                await client.call_tool(
                    "zoom",
                    {"screenshot_id": screenshot_id, "left": 300, "top": 0, "width": 50, "height": 10},
                )
            with pytest.raises(ToolError):
                await client.call_tool("observe_window", {"handle": 404})
            return screenshot_id

    screenshot_id = asyncio.run(call())

    listed, observed, zoomed, missing = desktop.action_log()
    assert (listed["tool"], listed["target"], listed["outcome"]) == ("list_windows", {}, "succeeded")
    assert (observed["tool"], observed["target"]) == ("observe_window", {"window": 0x1234})
    assert (observed["verdict"], observed["outcome"], observed["evidence"]) == (
        "allowed",
        "succeeded",
        None,
    )
    assert zoomed["tool"] == "zoom"
    assert zoomed["target"] == {
        "window": 0x1234,
        "screenshot_id": screenshot_id,
        "rect": {"left": 300, "top": 0, "width": 50, "height": 10},
    }
    assert zoomed["outcome"] == "failed"
    evidence = Image.open(io.BytesIO(desktop.evidence[zoomed["evidence"]]))
    assert evidence.size == (320, 240)
    assert (missing["target"], missing["outcome"], missing["evidence"]) == (
        {"window": 404},
        "failed",
        None,
    )
    assert "404" in missing["detail"]


def test_声明作用域与点击可经由_MCP_调用_拒绝与成功都记入日志() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="弹出的广告", process_name="ad.exe", rect=Rect(100, 50, 50, 50)),
            window(handle=1, title="无标题 - 记事本", rect=Rect(100, 50, 320, 240)),
        ],
        images={1: Image.new("RGB", (320, 240), "red")},
    )

    async def call() -> tuple[Any, Any, str, str]:
        async with Client(create_server(desktop)) as client:
            declared = await client.call_tool("declare_scope", {"handles": [1]})
            scope = await client.call_tool("get_scope", {})
            observed = await client.call_tool("observe_window", {"handle": 1})
            screenshot_id: str = observed.data["screenshot_id"]
            with pytest.raises(ToolError, match="任务作用域之外") as refused:
                await client.call_tool(
                    "click",
                    {"screenshot_id": screenshot_id, "x": 10, "y": 10, "intent": "点编辑区", "dangerous": False},
                )
            clicked = await client.call_tool(
                "click",
                {"screenshot_id": screenshot_id, "x": 100, "y": 100, "intent": "点编辑区", "dangerous": False},
            )
            assert declared.data == scope.data
            return scope.data, clicked.data, screenshot_id, str(refused.value)

    scope, clicked, screenshot_id, refusal = asyncio.run(call())

    assert scope == [{"handle": 1, "title": "无标题 - 记事本", "process_name": "notepad.exe"}]
    assert clicked["screen_point"] == {"x": 200, "y": 150}
    assert desktop.clicks == [(200, 150)]
    declared_record, _, _, refused_record, clicked_record = desktop.action_log()
    assert (declared_record["tool"], declared_record["target"]) == ("declare_scope", {"windows": [1]})
    assert declared_record["outcome"] == "succeeded"
    assert refused_record["tool"] == "click"
    assert refused_record["target"] == {"window": 1, "screenshot_id": screenshot_id, "x": 10, "y": 10}
    assert refused_record["intent"] == "点编辑区"
    assert (refused_record["verdict"], refused_record["outcome"]) == ("intercepted", "not_executed")
    assert refused_record["detail"] in refusal
    assert Image.open(io.BytesIO(desktop.evidence[refused_record["evidence"]])).size == (320, 240)
    assert (clicked_record["verdict"], clicked_record["outcome"]) == ("allowed", "succeeded")
    assert clicked_record["evidence"] is None


def test_危险点击经由_MCP_被拦截_带_confirmed_重试也不放行_经_hook_交人裁决后才执行() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="订单管理", rect=Rect(100, 50, 320, 240))],
        texts=[(Rect(300, 250, 40, 20), "删除")],
    )

    async def call() -> list[str]:
        refusals = []
        async with Client(create_server(desktop)) as client:
            await client.call_tool("declare_scope", {"handles": [1]})
            observed = await client.call_tool("observe_window", {"handle": 1})
            request = {
                "screenshot_id": observed.data["screenshot_id"],
                "x": 220,
                "y": 210,
                "intent": "删除这条订单",
                "dangerous": False,
            }
            for attempt in (
                request,
                {**request, "dangerous": True},
                {**request, "dangerous": True, "confirmed": True},
            ):
                with pytest.raises(ToolError) as refused:
                    await client.call_tool("click", attempt)
                refusals.append(str(refused.value))
            assert desktop.clicks == []
            approved = {**request, "dangerous": True}
            output = decide(desktop, _hook_payload("mcp__computer-use__click", approved))
            assert output is not None
            await client.call_tool("click", approved)
        return refusals

    refusals = asyncio.run(call())

    assert "删除" in refusals[0]
    assert "confirmed" in refusals[2]
    assert desktop.clicks == [(320, 260)]
    records = [r for r in desktop.action_log() if r["tool"] == "click"]
    # 带 `confirmed` 的那次在参数校验时就被拒，连工具都没进，因此不在日志里。
    assert [(r["dangerous"], r["verdict"]) for r in records] == [
        (False, "intercepted"),
        (True, "intercepted"),
        (True, "allowed"),
    ]


def _hook_payload(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": "session-1",
        "hook_event_name": "PreToolUse",
        "permission_mode": "default",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": "toolu_01",
    }


def test_声明不可操作的窗口经由_MCP_返回错误() -> None:
    async def call() -> None:
        async with Client(create_server(FakeDesktop())) as client:
            await client.call_tool("declare_scope", {"handles": [404]})

    with pytest.raises(ToolError, match="404"):
        asyncio.run(call())


def _image_size(result: CallToolResult) -> tuple[int, int]:
    [image] = [c for c in result.content if isinstance(c, ImageContent)]
    assert image.mime_type == "image/png"
    size: tuple[int, int] = Image.open(io.BytesIO(base64.b64decode(image.data))).size
    return size
