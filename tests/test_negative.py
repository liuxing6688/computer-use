"""负向套件：每项的通过条件是被拒绝，且拒绝理由说中了该说的那一件事。

走进程内的 MCP 工具层与 `FakeDesktop`，不碰真实桌面，干净的 Windows 机器上即可复现。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from PIL import Image, ImageDraw

from computer_use.desktop import Rect
from computer_use.hook import decide
from computer_use.server import create_server

from .fake_desktop import FakeDesktop, window


def test_越界点击被拒绝_理由指明落点窗口在任务作用域之外() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="弹出的广告", process_name="ad.exe", rect=Rect(0, 0, 80, 80)),
            window(handle=1, title="无标题 - 记事本", rect=Rect(0, 0, 320, 240)),
        ]
    )

    refusal = _refuse(
        desktop,
        prepare_handle=1,
        tool="click",
        arguments={"x": 10, "y": 10, "intent": "点编辑区", "dangerous": False},
    )

    assert "任务作用域之外" in refusal
    assert "弹出的广告" in refusal
    assert desktop.clicks == []


def test_高危窗口点击被拒绝_理由指明终端() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="管理员: Windows PowerShell", process_name="powershell.exe")]
    )

    refusal = _refuse(
        desktop,
        prepare_handle=1,
        tool="click",
        arguments={"x": 10, "y": 10, "intent": "点一下", "dangerous": False},
    )

    assert "高危窗口" in refusal
    assert "终端" in refusal
    assert desktop.clicks == []


def test_未确认的外发点击被拒绝_理由指明发送且对话框还没弹() -> None:
    button = Rect(left=600, top=400, width=60, height=24)
    desktop = FakeDesktop(
        [window(handle=1, title="微信", process_name="WeChat.exe", rect=Rect(0, 0, 800, 600))],
        texts=[(button, "发送")],
    )

    refusal = _refuse(
        desktop,
        prepare_handle=1,
        tool="click",
        arguments={"x": 630, "y": 412, "intent": "发送这条消息", "dangerous": True},
    )

    assert "发送" in refusal
    assert "没有经过人的裁决" in refusal
    assert desktop.clicks == []
    assert desktop.dialogs == []


def test_管道后的写命令被拒绝_理由指明整条命令含写操作() -> None:
    command = "Get-ChildItem | Remove-Item"
    desktop = FakeDesktop()

    refusal = _refuse(
        desktop,
        tool="run_powershell",
        arguments={"command": command, "intent": "清空目录", "dangerous": False},
    )

    assert "命令含写操作" in refusal
    assert command in _ask_reason(desktop, "run_powershell", {"command": command, "intent": "清空目录", "dangerous": False})
    assert desktop.commands == []


def test_未确认的删除被拒绝_理由指明删除且文件仍在() -> None:
    desktop = FakeDesktop(files={"E:/notes/a.txt": "hello", "E:/notes": None})
    arguments = {"path": "E:/notes/a.txt", "intent": "清掉草稿", "dangerous": False}

    refusal = _refuse(desktop, tool="delete_file", arguments=arguments)

    assert "删除文件须经人确认" in refusal
    reason = _ask_reason(desktop, "delete_file", arguments)
    assert "E:/notes/a.txt" in reason
    assert "移入回收站" in reason
    assert desktop.read_text("E:/notes/a.txt") == "hello"
    assert desktop.recycled == []


def test_覆盖写未提示覆盖时不能算通过_未经裁决的覆盖不落盘且确认写明覆盖() -> None:
    desktop = FakeDesktop(files={"E:/notes/a.txt": "old", "E:/notes": None})
    arguments = {
        "path": "E:/notes/a.txt",
        "content": "new",
        "intent": "改一句",
        "dangerous": False,
    }

    refusal = _refuse(desktop, tool="write_file", arguments=arguments)

    assert "写入文件须经人确认" in refusal
    reason = _ask_reason(desktop, "write_file", arguments)
    assert "E:/notes/a.txt" in reason
    assert "覆盖" in reason
    assert "变更类型：写入" not in reason
    assert desktop.read_text("E:/notes/a.txt") == "old"


def test_陈旧截图上的点击被拒绝_理由要求重新观察() -> None:
    desktop = _Repainting(
        [window(handle=1, rect=Rect(0, 0, 320, 480))],
        images={1: _bars([200, 80, 260, 140])},
    )

    async def call() -> str:
        async with Client(create_server(desktop)) as client:
            await client.call_tool("declare_scope", {"handles": [1]})
            observed = await client.call_tool("observe_window", {"handle": 1})
            desktop.repaint(1, _bars([150, 200, 80, 260, 140]))
            with pytest.raises(ToolError) as refused:
                await client.call_tool(
                    "click",
                    {
                        "screenshot_id": observed.data["screenshot_id"],
                        "x": 100,
                        "y": 140,
                        "intent": "点第四条消息",
                        "dangerous": False,
                    },
                )
            return str(refused.value)

    refusal = asyncio.run(call())

    assert "重新观察" in refusal
    assert desktop.clicks == []
    _assert_refused_log(desktop, "click", "重新观察")


def test_急停后继续点击被拒绝_理由指明输入工具已停() -> None:
    desktop = FakeDesktop([window(handle=1, title="无标题 - 记事本")])

    async def call() -> str:
        async with Client(create_server(desktop)) as client:
            await client.call_tool("declare_scope", {"handles": [1]})
            observed = await client.call_tool("observe_window", {"handle": 1})
            desktop.press_stop_hotkey()
            with pytest.raises(ToolError) as refused:
                await client.call_tool(
                    "click",
                    {
                        "screenshot_id": observed.data["screenshot_id"],
                        "x": 10,
                        "y": 10,
                        "intent": "再点一下",
                        "dangerous": False,
                    },
                )
            return str(refused.value)

    refusal = asyncio.run(call())

    assert "已急停" in refusal
    assert "输入工具拒绝执行" in refusal
    assert desktop.clicks == []
    _assert_refused_log(desktop, "click", "已急停")


class _Repainting(FakeDesktop):
    def repaint(self, handle: int, image: Image.Image) -> None:
        self._images[handle] = image


def _bars(widths: list[int]) -> Image.Image:
    image = Image.new("RGB", (320, 480), "white")
    draw = ImageDraw.Draw(image)
    for row, width in enumerate(widths):
        top = row * 40
        draw.rectangle((10, top + 12, 10 + width, top + 28), fill="black")
    return image


def _refuse(
    desktop: FakeDesktop,
    *,
    tool: str,
    arguments: dict[str, Any],
    prepare_handle: int | None = None,
) -> str:
    async def call() -> str:
        async with Client(create_server(desktop)) as client:
            if prepare_handle is not None:
                await client.call_tool("declare_scope", {"handles": [prepare_handle]})
                observed = await client.call_tool("observe_window", {"handle": prepare_handle})
                arguments["screenshot_id"] = observed.data["screenshot_id"]
            with pytest.raises(ToolError) as refused:
                await client.call_tool(tool, arguments)
            return str(refused.value)

    refusal = asyncio.run(call())
    _assert_refused_log(desktop, tool, refusal)
    return refusal


def _ask_reason(desktop: FakeDesktop, tool: str, arguments: dict[str, Any]) -> str:
    output = decide(desktop, {"tool_name": f"mcp__computer-use__{tool}", "tool_input": arguments})
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
    return str(output["hookSpecificOutput"]["permissionDecisionReason"])


def _assert_refused_log(desktop: FakeDesktop, tool: str, reason: str) -> None:
    record = next(item for item in reversed(desktop.action_log()) if item["tool"] == tool)
    assert (record["verdict"], record["outcome"]) == ("intercepted", "not_executed")
    assert record["detail"] is not None
    assert record["detail"] in reason or reason in record["detail"]
