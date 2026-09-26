"""危险动作判定与拦截。

模型自报为危险的动作只在经人裁决后执行。落点 OCR 命中高危词而模型自报不危险时，
同一次调用里用原生对话框问人。这是"写错了也看起来正常工作"的部分（ADR-0002），
负向用例与正向用例同等重要。
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import timedelta
from typing import Any

import pytest

from fastmcp import Client

from computer_use.action_log import Intercepted
from computer_use.danger import judge_call, judge_nearby_text
from computer_use.desktop import Rect
from computer_use.hook import decide, run
from computer_use.observation import Screenshots
from computer_use.scope import TaskScope
from computer_use.server import INSTRUCTIONS, create_server
from computer_use.tools import click, declare_scope, observe_window

from .fake_desktop import FakeDesktop, window
from .support import assert_no_retry_instruction

# ---- 判定：模型自报（hook 与服务端共用） ----


def test_模型自报为危险时判为危险() -> None:
    verdict = judge_call("click", _arguments(dangerous=True))

    assert verdict.dangerous
    assert any("自报" in reason for reason in verdict.reasons)


def test_模型自报不危险时不因自报判为危险() -> None:
    assert not judge_call("click", _arguments(dangerous=False)).dangerous


@pytest.mark.parametrize(
    "arguments",
    [
        {"screenshot_id": "shot-1", "x": 1, "y": 2, "intent": "点一下"},
        {"screenshot_id": "shot-1", "x": 1, "y": 2, "intent": "点", "dangerous": "false"},
        {"screenshot_id": "shot-1", "x": 1, "y": 2, "intent": "点", "dangerous": None},
    ],
    ids=["未自报", "自报不是布尔值", "自报为空"],
)
def test_未能明确自报不危险的输入工具调用判为危险(arguments: dict[str, Any]) -> None:
    assert judge_call("click", arguments).dangerous


@pytest.mark.parametrize("tool", ["list_windows", "observe_window", "zoom", "get_scope"])
def test_只读工具不因自报判为危险(tool: str) -> None:
    assert not judge_call(tool, {}).dangerous


# ---- 判定：落点附近的文字（服务端独有） ----


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("删 除", "删除"),
        ("卸载", "卸载"),
        ("发送(S)", "发送"),
        ("确 定 取 消", "确定"),
        ("立即支付", "支付"),
        ("提交订单", "提交"),
        ("永久删除", "删除"),
        ("Delete", "delete"),
        ("SEND", "send"),
        ("Pay now", "pay"),
        ("OK Cancel", "ok"),
    ],
)
def test_落点附近有高危词时判为危险_理由指明那个词(text: str, word: str) -> None:
    verdict = judge_nearby_text(text)

    assert verdict.dangerous
    assert any(word in reason.lower() for reason in verdict.reasons)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "文件 编辑 格式 查看 帮助",
        "File Edit Format View Help",
        "Book a table",
        "Sender: Alice",
        "Payment history",
        "取消",
    ],
)
def test_落点附近没有高危词时不判为危险(text: str) -> None:
    assert not judge_nearby_text(text).dangerous


def test_读不出落点附近的文字时不因此判为危险() -> None:
    assert not judge_nearby_text(None).dangerous


def test_两条判据取并集() -> None:
    self_reported = judge_call("click", _arguments(dangerous=True))
    nearby = judge_nearby_text("删除")
    neither = judge_call("click", _arguments(dangerous=False)) | judge_nearby_text("取消")

    assert (self_reported | judge_nearby_text("取消")).dangerous
    assert (judge_call("click", _arguments(dangerous=False)) | nearby).dangerous
    assert set((self_reported | nearby).reasons) == set(self_reported.reasons + nearby.reasons)
    assert not neither.dangerous


# ---- 服务端：判为危险且未经人裁决的点击不抵达桌面 ----


DELETE_BUTTON = Rect(left=600, top=400, width=60, height=24)


def _desktop(**kwargs: Any) -> FakeDesktop:
    return FakeDesktop(
        [window(handle=1, title="订单管理", rect=Rect(0, 0, 800, 600))],
        texts=kwargs.pop("texts", [(DELETE_BUTTON, "删 除")]),
        **kwargs,
    )


def _ready(desktop: FakeDesktop) -> tuple[Screenshots, TaskScope, str]:
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]
    return screenshots, scope, screenshot_id


def test_模型自报不危险但落点附近有高危词时_同一次调用经人确认后才点击() -> None:
    desktop = _desktop()
    screenshots, scope, screenshot_id = _ready(desktop)

    click(desktop, screenshots, scope, screenshot_id, 630, 412, intent="点这一行", dangerous=False)

    assert desktop.clicks == [(630, 412)]
    assert desktop.tickets == {}
    [dialog] = desktop.dialogs
    assert dialog.timeout == 60
    assert "删除" in dialog.message
    assert "点这一行" in dialog.message


def test_人拒绝高危词确认时不点击_也不要求改标志重试() -> None:
    desktop = _desktop()
    desktop.dialog_reply = False
    screenshots, scope, screenshot_id = _ready(desktop)

    with pytest.raises(Intercepted, match="拒绝") as intercepted:
        click(desktop, screenshots, scope, screenshot_id, 630, 412, intent="点这一行", dangerous=False)

    assert_no_retry_instruction(str(intercepted.value))
    assert desktop.clicks == []
    assert len(desktop.dialogs) == 1


def test_高危词确认超时按拒绝处理且不点击() -> None:
    desktop = _desktop()
    desktop.dialog_reply = None
    screenshots, scope, screenshot_id = _ready(desktop)

    with pytest.raises(Intercepted, match="超时") as intercepted:
        click(desktop, screenshots, scope, screenshot_id, 630, 412, intent="点这一行", dangerous=False)

    message = str(intercepted.value)
    assert "拒绝" in message
    assert_no_retry_instruction(message)
    assert desktop.clicks == []


def test_高危词离落点较远时不因它拦截() -> None:
    desktop = _desktop()
    screenshots, scope, screenshot_id = _ready(desktop)

    click(desktop, screenshots, scope, screenshot_id, 100, 100, intent="点编辑区", dangerous=False)

    assert desktop.clicks == [(100, 100)]


def test_说明不再指示模型因高危词改标志重试() -> None:
    start = INSTRUCTIONS.index("高危词")
    sentence = INSTRUCTIONS[start : INSTRUCTIONS.index("。", start) + 1]

    assert "对话框" in sentence
    assert_no_retry_instruction(sentence)

    async def description() -> str:
        async with Client(create_server(FakeDesktop())) as client:
            tools = await client.list_tools()
        click_tool = next(tool for tool in tools if tool.name == "click")
        return click_tool.description or ""

    click_description = asyncio.run(description())
    start = click_description.index("高危词")
    sentence = click_description[start : click_description.index("。", start) + 1]
    assert "对话框" in sentence
    assert_no_retry_instruction(sentence)


def test_模型自报为危险时高危词仍走裁决凭据_不弹原生对话框() -> None:
    desktop = _desktop()
    screenshots, scope, screenshot_id = _ready(desktop)

    with pytest.raises(Intercepted, match="裁决"):
        click(desktop, screenshots, scope, screenshot_id, 630, 412, intent="点删除按钮", dangerous=True)

    assert desktop.dialogs == []
    assert desktop.clicks == []


def test_模型自报为危险而未经人裁决时拦截_原样重试也拦截() -> None:
    desktop = _desktop(texts=[])
    screenshots, scope, screenshot_id = _ready(desktop)

    for _ in range(3):
        with pytest.raises(Intercepted, match="人"):
            click(desktop, screenshots, scope, screenshot_id, 100, 100, intent="清空回收站", dangerous=True)

    assert desktop.clicks == []


def test_读不出落点附近的文字且模型自报不危险时不因此拦截_点击得以执行() -> None:
    desktop = _desktop(texts=[], unreadable=True)
    screenshots, scope, screenshot_id = _ready(desktop)

    click(desktop, screenshots, scope, screenshot_id, 100, 100, intent="点编辑区", dangerous=False)

    assert desktop.clicks == [(100, 100)]


def test_读不出落点附近的文字时_模型自报危险仍须经人确认() -> None:
    desktop = _desktop(texts=[], unreadable=True)
    screenshots, scope, screenshot_id = _ready(desktop)

    with pytest.raises(Intercepted, match="人"):
        click(desktop, screenshots, scope, screenshot_id, 100, 100, intent="点编辑区", dangerous=True)

    assert desktop.clicks == []


# ---- hook：自报为危险的调用交给人裁决，并留下裁决凭据 ----


def _payload(tool_input: dict[str, Any], tool: str = "click") -> dict[str, Any]:
    """Claude Code 在 PreToolUse 时写进 hook 标准输入的那份 JSON。"""

    return {
        "session_id": "session-1",
        "transcript_path": "C:/transcript.jsonl",
        "cwd": "E:/",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": f"mcp__computer-use__{tool}",
        "tool_input": tool_input,
        "tool_use_id": "toolu_01",
    }


def test_hook_把自报为危险的调用交给人裁决_理由含意图与落点() -> None:
    output = decide(FakeDesktop(), _payload(_arguments(dangerous=True, intent="删除这条订单")))

    assert output is not None
    specific = output["hookSpecificOutput"]
    assert (specific["hookEventName"], specific["permissionDecision"]) == ("PreToolUse", "ask")
    assert "删除这条订单" in specific["permissionDecisionReason"]
    assert "(630, 412)" in specific["permissionDecisionReason"]


def test_hook_对未自报危险性的输入工具调用同样交给人裁决() -> None:
    arguments = _arguments(dangerous=False)
    del arguments["dangerous"]

    output = decide(FakeDesktop(), _payload(arguments))

    assert output is not None and output["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_hook_放行自报不危险的调用与只读工具_不留凭据() -> None:
    desktop = FakeDesktop()

    assert decide(desktop, _payload(_arguments(dangerous=False))) is None
    assert decide(desktop, _payload({}, tool="list_windows")) is None
    assert decide(desktop, _payload({"handle": 1}, tool="observe_window")) is None
    assert desktop.tickets == {}


def test_经人裁决后危险点击得以执行() -> None:
    desktop = _desktop()
    screenshots, scope, screenshot_id = _ready(desktop)
    arguments = _arguments(screenshot_id=screenshot_id, intent="删除这条订单", dangerous=True)

    decide(desktop, _payload(arguments))
    click(desktop, screenshots, scope, **arguments)

    assert desktop.clicks == [(630, 412)]


def test_一次裁决只放行一次() -> None:
    desktop = _desktop()
    screenshots, scope, screenshot_id = _ready(desktop)
    arguments = _arguments(screenshot_id=screenshot_id, dangerous=True)
    decide(desktop, _payload(arguments))
    click(desktop, screenshots, scope, **arguments)

    with pytest.raises(Intercepted):
        click(desktop, screenshots, scope, **arguments)

    assert desktop.clicks == [(630, 412)]


@pytest.mark.parametrize(
    "changed",
    [{"x": 631}, {"y": 400}, {"intent": "别的事"}],
    ids=["换了横坐标", "换了纵坐标", "换了意图"],
)
def test_裁决只对那一次调用的参数有效(changed: dict[str, Any]) -> None:
    desktop = _desktop()
    screenshots, scope, screenshot_id = _ready(desktop)
    arguments = _arguments(screenshot_id=screenshot_id, dangerous=True)
    decide(desktop, _payload(arguments))

    with pytest.raises(Intercepted):
        click(desktop, screenshots, scope, **{**arguments, **changed})

    assert desktop.clicks == []


def test_过期的裁决不再放行() -> None:
    desktop = _desktop()
    screenshots, scope, screenshot_id = _ready(desktop)
    arguments = _arguments(screenshot_id=screenshot_id, dangerous=True)
    decide(desktop, _payload(arguments))
    [key] = desktop.tickets
    desktop.tickets[key] -= timedelta(minutes=11)

    with pytest.raises(Intercepted):
        click(desktop, screenshots, scope, **arguments)

    assert desktop.clicks == []


def test_裁决不能让落到任务作用域之外的点击放行() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="弹出的广告", process_name="ad.exe", rect=Rect(600, 400, 100, 50)),
            window(handle=1, title="订单管理", rect=Rect(0, 0, 800, 600)),
        ]
    )
    screenshots, scope, screenshot_id = _ready(desktop)
    arguments = _arguments(screenshot_id=screenshot_id, dangerous=True)
    decide(desktop, _payload(arguments))

    with pytest.raises(Intercepted, match="任务作用域之外"):
        click(desktop, screenshots, scope, **arguments)

    assert desktop.clicks == []


def test_hook_从标准输入读调用_向标准输出写裁决() -> None:
    desktop = FakeDesktop()
    asked, passed = io.StringIO(), io.StringIO()

    run(desktop, io.StringIO(json.dumps(_payload(_arguments(dangerous=True)))), asked)
    run(desktop, io.StringIO(json.dumps(_payload(_arguments(dangerous=False)))), passed)

    assert json.loads(asked.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert passed.getvalue() == ""


def _arguments(
    *,
    screenshot_id: str = "shot-1",
    x: int = 630,
    y: int = 412,
    intent: str = "点删除按钮",
    dangerous: bool,
) -> dict[str, Any]:
    return {"screenshot_id": screenshot_id, "x": x, "y": y, "intent": intent, "dangerous": dangerous}
