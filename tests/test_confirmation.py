"""原生确认对话框：外发动作与切换任务作用域在执行前交人，模型无法参与。

对话框本身由 `DesktopPort` 弹出。测试替身记下弹了什么、并按脚本回答，无需人工点击。
"""

from __future__ import annotations

from typing import Any

import pytest
from PIL import Image

from computer_use.action_log import ActionLog, Intercepted
from computer_use.desktop import Rect
from computer_use.hook import decide
from computer_use.observation import Screenshots
from computer_use.scope import TaskScope
from computer_use.tools import click, declare_scope, get_scope, observe_window, press_keys, type_text

from .fake_desktop import FakeDesktop, window
from .support import assert_no_retry_instruction

SEND_BUTTON = Rect(left=600, top=400, width=60, height=24)


def test_用户拒绝外发点击时不执行并记入日志() -> None:
    desktop = _desktop()
    desktop.dialog_reply = False
    screenshots, scope, screenshot_id = _ready(desktop)
    arguments = _arguments(screenshot_id=screenshot_id, intent="发送这条消息")
    decide(desktop, _payload(arguments))

    with pytest.raises(Intercepted, match="拒绝"):
        _logged_click(desktop, screenshots, scope, arguments)

    assert desktop.clicks == []
    [dialog] = desktop.dialogs
    assert dialog.timeout == 60
    assert "发送" in dialog.message
    [record] = [item for item in desktop.action_log() if item["tool"] == "click"]
    assert (record["verdict"], record["outcome"]) == ("intercepted", "not_executed")
    assert record["detail"] is not None and "拒绝" in record["detail"]


def test_外发确认超时按拒绝处理并记入日志() -> None:
    desktop = _desktop()
    desktop.dialog_reply = None
    screenshots, scope, screenshot_id = _ready(desktop)
    arguments = _arguments(screenshot_id=screenshot_id, intent="发送这条消息")
    decide(desktop, _payload(arguments))

    with pytest.raises(Intercepted, match="超时"):
        _logged_click(desktop, screenshots, scope, arguments)

    assert desktop.clicks == []
    [record] = [item for item in desktop.action_log() if item["tool"] == "click"]
    assert record["outcome"] == "not_executed"
    assert record["detail"] is not None and "超时" in record["detail"] and "拒绝" in record["detail"]


def test_人允许后外发点击才执行() -> None:
    desktop = _desktop()
    screenshots, scope, screenshot_id = _ready(desktop)
    arguments = _arguments(screenshot_id=screenshot_id, intent="发送这条消息")
    decide(desktop, _payload(arguments))

    _logged_click(desktop, screenshots, scope, arguments)

    assert desktop.clicks == [(630, 412)]
    assert len(desktop.dialogs) == 1


def test_发送类确认同时呈现当前窗口截图与已输入内容() -> None:
    frame = Image.new("RGB", (800, 600), "red")
    desktop = _desktop(images={1: frame})
    screenshots, scope, screenshot_id = _ready(desktop)
    type_text(
        desktop, screenshots, scope, screenshot_id, "明天见",
        intent="填进输入框", dangerous=False,
    )
    arguments = _arguments(screenshot_id=screenshot_id, intent="发送这条消息")
    decide(desktop, _payload(arguments))

    click(
        desktop, screenshots, scope, screenshot_id, 630, 412,
        intent="发送这条消息", dangerous=True,
    )

    [dialog] = desktop.dialogs
    assert dialog.image is frame
    assert "明天见" in dialog.message
    assert "（无）" not in dialog.message


def test_变化说明采集失败时已打进的文字仍出现在发送确认里() -> None:
    desktop = _desktop()
    screenshots, scope, screenshot_id = _ready(desktop)
    desktop.fail_observation_after_input = True

    with pytest.raises(OSError, match="采集不了当前桌面"):
        type_text(
            desktop, screenshots, scope, screenshot_id, "明天见",
            intent="填进输入框", dangerous=False,
        )

    assert desktop.pasted == ["明天见"]
    arguments = _arguments(screenshot_id=screenshot_id, intent="发送这条消息")
    decide(desktop, _payload(arguments))
    click(
        desktop, screenshots, scope, screenshot_id, 630, 412,
        intent="发送这条消息", dangerous=True,
    )

    [dialog] = desktop.dialogs
    assert "明天见" in dialog.message
    assert "（无）" not in dialog.message


def test_支付确认不附带已输入内容() -> None:
    desktop = _desktop(texts=[(SEND_BUTTON, "支付")])
    screenshots, scope, screenshot_id = _ready(desktop)
    type_text(
        desktop, screenshots, scope, screenshot_id, "100",
        intent="填金额", dangerous=False,
    )
    arguments = _arguments(screenshot_id=screenshot_id, intent="去支付")
    decide(desktop, _payload(arguments))

    click(
        desktop, screenshots, scope, screenshot_id, 630, 412,
        intent="去支付", dangerous=True,
    )

    [dialog] = desktop.dialogs
    assert dialog.image is None
    assert "支付" in dialog.message
    assert "100" not in dialog.message
    assert "已输入的内容" not in dialog.message


def test_删除经过裁决后执行且不弹原生对话框() -> None:
    desktop = _desktop(texts=[(SEND_BUTTON, "删除")])
    screenshots, scope, screenshot_id = _ready(desktop)
    arguments = _arguments(screenshot_id=screenshot_id, intent="删除这条")
    decide(desktop, _payload(arguments))

    click(
        desktop, screenshots, scope, screenshot_id, 630, 412,
        intent="删除这条", dangerous=True,
    )

    assert desktop.dialogs == []
    assert desktop.clicks == [(630, 412)]


def test_模型把外发自报成不危险时同一次调用弹原生对话框_人确认后才执行() -> None:
    desktop = _desktop()
    screenshots, scope, screenshot_id = _ready(desktop)

    click(
        desktop, screenshots, scope, screenshot_id, 630, 412,
        intent="点一下", dangerous=False,
    )

    assert desktop.clicks == [(630, 412)]
    assert desktop.tickets == {}
    [dialog] = desktop.dialogs
    assert dialog.timeout == 60
    assert "发送" in dialog.message
    assert dialog.image is not None
    assert "已输入的内容" in dialog.message


def test_模型把外发自报成不危险时人拒绝则不执行_也不要求改标志重试() -> None:
    desktop = _desktop()
    desktop.dialog_reply = False
    screenshots, scope, screenshot_id = _ready(desktop)

    with pytest.raises(Intercepted, match="拒绝") as intercepted:
        click(
            desktop, screenshots, scope, screenshot_id, 630, 412,
            intent="点一下", dangerous=False,
        )

    assert_no_retry_instruction(str(intercepted.value))
    assert desktop.clicks == []
    assert len(desktop.dialogs) == 1


def test_打进窗口的文字本身不弹确认() -> None:
    desktop = _desktop()
    screenshots, scope, screenshot_id = _ready(desktop)

    type_text(
        desktop, screenshots, scope, screenshot_id, "请发送会议纪要",
        intent="填进输入框", dangerous=False,
    )

    assert desktop.dialogs == []
    assert desktop.pasted == ["请发送会议纪要"]


def test_以发送为意图的按键在拒绝时不送出() -> None:
    desktop = _desktop(texts=[])
    screenshots, scope, screenshot_id = _ready(desktop)
    arguments = {
        "screenshot_id": screenshot_id,
        "keys": ["enter"],
        "intent": "发送这条消息",
        "dangerous": True,
    }
    decide(desktop, _payload(arguments, tool="press_keys"))
    desktop.dialog_reply = False

    with pytest.raises(Intercepted, match="拒绝"):
        press_keys(
            desktop, screenshots, scope, screenshot_id, ["enter"],
            intent="发送这条消息", dangerous=True,
        )

    assert desktop.chords == []
    assert "发送" in desktop.dialogs[0].message


def test_第一次声明任务作用域不弹对话框() -> None:
    desktop = _desktop()

    declare_scope(desktop, TaskScope(), [1])

    assert desktop.dialogs == []


def test_用户拒绝切换任务作用域时作用域不变并记入日志() -> None:
    desktop = _two_windows()
    scope = TaskScope()
    declare_scope(desktop, scope, [1])
    desktop.dialog_reply = False

    with pytest.raises(Intercepted, match="拒绝"):
        ActionLog(desktop).run(
            tool="declare_scope",
            target={"windows": [2]},
            intent=None,
            dangerous=None,
            evidence_window=None,
            action=lambda: declare_scope(desktop, scope, [2]),
        )

    assert get_scope(scope) == [
        {
            "handle": 1,
            "title": "<untrusted-screen>微信</untrusted-screen>",
            "process_name": "WeChat.exe",
        }
    ]
    [record] = [item for item in desktop.action_log() if item["tool"] == "declare_scope"]
    assert (record["verdict"], record["outcome"]) == ("intercepted", "not_executed")
    assert record["detail"] is not None and "拒绝" in record["detail"] and "未改变" in record["detail"]


def test_切换作用域超时按拒绝且作用域不变() -> None:
    desktop = _two_windows()
    scope = TaskScope()
    declare_scope(desktop, scope, [1])
    desktop.dialog_reply = None

    with pytest.raises(Intercepted, match="超时"):
        declare_scope(desktop, scope, [2])

    assert [item["handle"] for item in get_scope(scope)] == [1]
    assert desktop.dialogs[0].timeout == 60


def test_人允许后任务作用域换成另一组窗口() -> None:
    desktop = _two_windows()
    scope = TaskScope()
    declare_scope(desktop, scope, [1])

    declared = declare_scope(desktop, scope, [2])

    assert [item["handle"] for item in declared] == [2]
    assert "记事本" in desktop.dialogs[0].message
    assert "微信" in desktop.dialogs[0].message
    assert "<untrusted-screen>" not in desktop.dialogs[0].message


def test_原样再次声明不弹对话框() -> None:
    desktop = _desktop()
    scope = TaskScope()
    declare_scope(desktop, scope, [1])

    declare_scope(desktop, scope, [1])

    assert desktop.dialogs == []


def _logged_click(
    desktop: FakeDesktop,
    screenshots: Screenshots,
    scope: TaskScope,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    screenshot_id = arguments["screenshot_id"]
    return ActionLog(desktop).run(
        tool="click",
        target={
            "window": 1,
            "screenshot_id": screenshot_id,
            "x": arguments["x"],
            "y": arguments["y"],
        },
        intent=arguments["intent"],
        dangerous=arguments["dangerous"],
        evidence_window=1,
        action=lambda: click(
            desktop,
            screenshots,
            scope,
            screenshot_id,
            arguments["x"],
            arguments["y"],
            intent=arguments["intent"],
            dangerous=arguments["dangerous"],
        ),
    )


def _two_windows() -> FakeDesktop:
    return FakeDesktop(
        [
            window(handle=2, title="无标题 - 记事本", process_name="notepad.exe"),
            window(handle=1, title="微信", process_name="WeChat.exe"),
        ]
    )


def _desktop(**kwargs: Any) -> FakeDesktop:
    return FakeDesktop(
        [window(handle=1, title="微信", process_name="WeChat.exe", rect=Rect(0, 0, 800, 600))],
        texts=kwargs.pop("texts", [(SEND_BUTTON, "发送")]),
        **kwargs,
    )


def _ready(desktop: FakeDesktop) -> tuple[Screenshots, TaskScope, str]:
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]
    return screenshots, scope, screenshot_id


def _arguments(
    *,
    screenshot_id: str,
    x: int = 630,
    y: int = 412,
    intent: str,
    dangerous: bool = True,
) -> dict[str, Any]:
    return {
        "screenshot_id": screenshot_id,
        "x": x,
        "y": y,
        "intent": intent,
        "dangerous": dangerous,
    }


def _payload(tool_input: dict[str, Any], tool: str = "click") -> dict[str, Any]:
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
