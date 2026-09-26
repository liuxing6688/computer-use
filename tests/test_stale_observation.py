"""截图比对：执行前重新采集落点周围，与模型决策时那张截图比对，界面自己动了就拒绝。"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from PIL import Image, ImageDraw

from computer_use.action_log import Intercepted
from computer_use.desktop import Rect, RegionChange
from computer_use.observation import ObservationError, Screenshots
from computer_use.scope import TaskScope
from computer_use.server import create_server
from computer_use import tools
from computer_use.tools import declare_scope, observe_window, zoom

from .fake_desktop import FakeDesktop, window


def click(
    desktop: FakeDesktop,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
) -> dict[str, Any]:
    """一次模型自报不危险、落点附近也没有高危词的点击；危险判定见 `test_danger.py`。"""

    return tools.click(
        desktop, screenshots, scope, screenshot_id, x, y, intent="点一下", dangerous=False
    )

ROW_HEIGHT = 40
MESSAGES = [200, 80, 260, 140, 40, 220, 120, 180, 60, 240, 100, 160]


def _chat_list(messages: Sequence[int], size: tuple[int, int] = (320, 480)) -> Image.Image:
    """一个消息列表：每行一条消息，画成一段与消息长度等宽的黑条。"""

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for row, width in enumerate(messages):
        top = row * ROW_HEIGHT
        draw.rectangle((10, top + 12, 10 + width, top + 28), fill="black")
    return image


def _observe_then_change(
    before: Image.Image, after: Image.Image, rect: Rect = Rect(0, 0, 320, 480)
) -> tuple[FakeDesktop, Screenshots, TaskScope, str]:
    """在 `before` 的画面上观察，返回画面已变成 `after` 的桌面，以及那张截图的 ID。"""

    screenshots, scope = Screenshots(), TaskScope()
    observed = FakeDesktop([window(handle=1, rect=rect)], images={1: before})
    declare_scope(observed, scope, [1])
    screenshot_id: str = observe_window(observed, screenshots, 1).metadata["screenshot_id"]
    changed = FakeDesktop([window(handle=1, rect=rect)], images={1: after})
    return changed, screenshots, scope, screenshot_id


def test_新消息把列表顶下去一格时点击被拒绝_提示重新观察() -> None:
    desktop, screenshots, scope, screenshot_id = _observe_then_change(
        _chat_list(MESSAGES), _chat_list([150, *MESSAGES])
    )

    with pytest.raises(Intercepted, match="重新观察"):
        click(desktop, screenshots, scope, screenshot_id, 100, 3 * ROW_HEIGHT + 20)

    assert desktop.clicks == []


def test_界面没变时照常点击() -> None:
    desktop, screenshots, scope, screenshot_id = _observe_then_change(
        _chat_list(MESSAGES), _chat_list(MESSAGES)
    )

    click(desktop, screenshots, scope, screenshot_id, 100, 3 * ROW_HEIGHT + 20)

    assert desktop.clicks == [(100, 140)]


def test_替身指定目标区域已变化时_相同像素也会拒绝点击() -> None:
    desktop = FakeDesktop([window(handle=1)])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]
    desktop.region_change = RegionChange(changed=True, changed_pixels=40, total_pixels=40)

    with pytest.raises(Intercepted, match=r"40/40 个像素不同.*重新观察"):
        click(desktop, screenshots, scope, screenshot_id, 10, 10)

    assert desktop.clicks == []


def test_替身指定目标区域未变化时_像素不同也照常点击() -> None:
    before = Image.new("RGB", (8, 8), "white")
    after = Image.new("RGB", (8, 8), "black")
    desktop, screenshots, scope, screenshot_id = _observe_then_change(
        before, after, Rect(0, 0, 8, 8)
    )
    desktop.region_change = RegionChange(changed=False, changed_pixels=0, total_pixels=64)

    click(desktop, screenshots, scope, screenshot_id, 2, 2)

    assert desktop.clicks == [(2, 2)]


def test_光标闪烁不算界面变化_照常点击() -> None:
    before = _chat_list(MESSAGES)
    after = before.copy()
    ImageDraw.Draw(after).rectangle((160, 130, 161, 153), fill="black")

    desktop, screenshots, scope, screenshot_id = _observe_then_change(before, after)
    click(desktop, screenshots, scope, screenshot_id, 150, 140)

    assert desktop.clicks == [(150, 140)]


def test_鼠标悬停带来的轻微变色不算界面变化_照常点击() -> None:
    before = _chat_list(MESSAGES)
    after = before.copy()
    ImageDraw.Draw(after).rectangle((0, 120, 319, 159), fill=(235, 235, 235))
    ImageDraw.Draw(after).rectangle((10, 132, 10 + MESSAGES[3], 148), fill="black")

    desktop, screenshots, scope, screenshot_id = _observe_then_change(before, after)
    click(desktop, screenshots, scope, screenshot_id, 250, 140)

    assert desktop.clicks == [(250, 140)]


def test_变化远离落点时照常点击() -> None:
    before = _chat_list(MESSAGES)
    after = before.copy()
    ImageDraw.Draw(after).rectangle((0, 400, 319, 479), fill="black")

    desktop, screenshots, scope, screenshot_id = _observe_then_change(before, after)
    click(desktop, screenshots, scope, screenshot_id, 100, 60)

    assert desktop.clicks == [(100, 60)]


def test_落点靠近窗口边缘时仍能比对出变化() -> None:
    desktop, screenshots, scope, screenshot_id = _observe_then_change(
        _chat_list(MESSAGES), _chat_list([150, *MESSAGES])
    )

    with pytest.raises(Intercepted, match="重新观察"):
        click(desktop, screenshots, scope, screenshot_id, 200, 470)

    assert desktop.clicks == []


def test_窗口移动后点击被拒绝_提示重新观察() -> None:
    screenshots, scope = Screenshots(), TaskScope()
    before = FakeDesktop([window(handle=1, rect=Rect(0, 0, 320, 480))])
    declare_scope(before, scope, [1])
    screenshot_id: str = observe_window(before, screenshots, 1).metadata["screenshot_id"]
    moved = FakeDesktop([window(handle=1, rect=Rect(0, 40, 320, 480))])

    with pytest.raises(Intercepted, match="移动或改变大小.*重新观察"):
        click(moved, screenshots, scope, screenshot_id, 100, 100)

    assert moved.clicks == []


def test_截图之后弹出的对话框挡住落点时点击被拒绝_提示重新观察() -> None:
    screenshots, scope = Screenshots(), TaskScope()
    main = window(handle=1, rect=Rect(0, 0, 320, 480))
    before = FakeDesktop([main])
    declare_scope(before, scope, [1])
    screenshot_id: str = observe_window(before, screenshots, 1).metadata["screenshot_id"]
    popped = FakeDesktop(
        [window(handle=2, title="确认", owner=1, process_id=1, rect=Rect(50, 50, 200, 100)), main]
    )

    with pytest.raises(Intercepted, match="确认.*重新观察"):
        click(popped, screenshots, scope, screenshot_id, 100, 100)

    assert popped.clicks == []


def test_放大图上的点击同样与原始采集比对() -> None:
    desktop, screenshots, scope, screenshot_id = _observe_then_change(
        _chat_list(MESSAGES), _chat_list([150, *MESSAGES])
    )
    zoomed = zoom(screenshots, screenshot_id, Rect(0, 100, 160, 80))

    with pytest.raises(Intercepted, match="重新观察"):
        click(desktop, screenshots, scope, zoomed.metadata["screenshot_id"], 100, 40)

    assert desktop.clicks == []


class _Repainting(FakeDesktop):
    """画面可以在两次调用之间换掉的桌面。"""

    def repaint(self, handle: int, image: Image.Image) -> None:
        self._images[handle] = image


def test_陈旧观察上的点击经由_MCP_被拒绝_记为未执行并留证() -> None:
    desktop = _Repainting(
        [window(handle=1, rect=Rect(0, 0, 320, 480))], images={1: _chat_list(MESSAGES)}
    )

    async def call() -> str:
        async with Client(create_server(desktop)) as client:
            await client.call_tool("declare_scope", {"handles": [1]})
            observed = await client.call_tool("observe_window", {"handle": 1})
            desktop.repaint(1, _chat_list([150, *MESSAGES]))
            arguments = {"screenshot_id": observed.data["metadata"]["screenshot_id"], "x": 100, "y": 140}
            with pytest.raises(ToolError, match="重新观察") as refused:
                await client.call_tool("click", {**arguments, "intent": "点第四条消息", "dangerous": False})
            return str(refused.value)

    refusal = asyncio.run(call())

    assert desktop.clicks == []
    refused_record = desktop.action_log()[-1]
    assert (refused_record["verdict"], refused_record["outcome"]) == ("intercepted", "not_executed")
    assert refused_record["detail"] in refusal
    assert refused_record["evidence"] in desktop.evidence


def test_已过期的截图_ID_被拒绝_不注入点击() -> None:
    desktop = FakeDesktop([window(handle=1)])
    screenshots, scope = Screenshots(capacity=2), TaskScope()
    declare_scope(desktop, scope, [1])
    expired: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]
    observe_window(desktop, screenshots, 1)
    observe_window(desktop, screenshots, 1)

    with pytest.raises(ObservationError, match="过期"):
        click(desktop, screenshots, scope, expired, 10, 10)

    assert desktop.clicks == []
