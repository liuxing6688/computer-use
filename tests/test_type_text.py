"""文本输入：剪贴板粘贴优先，失败时逐字符注入，并在事后恢复剪贴板。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from computer_use.action_log import Intercepted
from computer_use.desktop import ClipboardUnavailable, ForegroundError, InjectionError, Rect
from computer_use.hook import decide
from computer_use.observation import ObservationError, Screenshots
from computer_use.scope import TaskScope
from computer_use.server import create_server
from computer_use.tools import declare_scope, observe_window, type_text

from .fake_desktop import FakeDesktop, window


def _observed(desktop: FakeDesktop, screenshots: Screenshots, handle: int) -> str:
    screenshot_id: str = observe_window(desktop, screenshots, handle).metadata["screenshot_id"]
    return screenshot_id


def test_中文经剪贴板粘贴进焦点输入框_原剪贴板内容被恢复() -> None:
    original = object()
    desktop = FakeDesktop([window(handle=1, title="无标题 - 记事本")])
    desktop.clipboard = original
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    result = type_text(
        desktop,
        screenshots,
        scope,
        _observed(desktop, screenshots, 1),
        "你好",
        intent="填写姓名",
        dangerous=False,
    )

    assert result == {
        "window": {
            "handle": 1,
            "title": "<untrusted-screen>无标题 - 记事本</untrusted-screen>",
            "process_name": "notepad.exe",
        },
        "tier": "clipboard",
        "clipboard_used": True,
    }
    assert desktop.pasted == ["你好"]
    assert desktop.characters == []
    assert desktop.clipboard is original
    assert [step[0] for step in desktop.trace] == [
        "focus",
        "read_clipboard",
        "set_clipboard_text",
        "paste",
        "restore_clipboard",
    ]
    assert desktop.trace[0] == ("focus", 1)


def test_粘贴失败时改为逐字符注入_剪贴板仍被恢复() -> None:
    original = object()
    desktop = FakeDesktop([window(handle=1)])
    desktop.clipboard = original
    desktop.paste_fails = True
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    result = type_text(
        desktop,
        screenshots,
        scope,
        _observed(desktop, screenshots, 1),
        "你好",
        intent="填写姓名",
        dangerous=False,
    )

    assert result["tier"] == "unicode"
    assert result["clipboard_used"] is True
    assert desktop.pasted == []
    assert desktop.characters == ["你", "好"]
    assert desktop.clipboard is original
    names = [step[0] for step in desktop.trace]
    assert names.index("focus") < names.index("paste") < names.index("restore_clipboard")
    assert names.index("restore_clipboard") < names.index("type_character")


def test_剪贴板读不出来时不改剪贴板_改为逐字符注入() -> None:
    original = object()
    ready = _scoped()
    ready.desktop.clipboard = original
    ready.desktop.clipboard_read_fails = True

    result = _enter(ready, "你好")

    assert (result["tier"], result["clipboard_used"]) == ("unicode", False)
    assert ready.desktop.clipboard is original
    assert ready.desktop.characters == ["你", "好"]
    assert "set_clipboard_text" not in [step[0] for step in ready.desktop.trace]
    assert "paste" not in [step[0] for step in ready.desktop.trace]


def test_剪贴板写不进去时不占用剪贴板_改为逐字符注入() -> None:
    original = object()
    ready = _scoped()
    ready.desktop.clipboard = original
    ready.desktop.clipboard_write_fails = True

    result = _enter(ready, "你好")

    assert (result["tier"], result["clipboard_used"]) == ("unicode", False)
    assert ready.desktop.clipboard is original
    assert ready.desktop.pasted == []
    assert ready.desktop.characters == ["你", "好"]


def test_目标窗口不能来到前台时不输入_剪贴板不动() -> None:
    original = object()
    ready = _scoped()
    ready.desktop.clipboard = original
    ready.desktop.focus_fails = True

    with pytest.raises(ForegroundError, match="前台"):
        _enter(ready, "你好")

    assert ready.desktop.clipboard is original
    assert ready.desktop.pasted == []
    assert ready.desktop.characters == []
    assert [step[0] for step in ready.desktop.trace] == ["focus"]


def test_粘贴成功但剪贴板恢复失败时不再注入() -> None:
    ready = _scoped()
    ready.desktop.clipboard = "用户复制的"
    ready.desktop.clipboard_restore_fails = True

    with pytest.raises(ClipboardUnavailable, match="没能恢复"):
        _enter(ready, "你好")

    assert ready.desktop.pasted == ["你好"]
    assert ready.desktop.characters == []


def test_粘贴失败且剪贴板恢复失败时不降级注入() -> None:
    ready = _scoped()
    ready.desktop.clipboard = "用户复制的"
    ready.desktop.paste_fails = True
    ready.desktop.clipboard_restore_fails = True

    with pytest.raises(ClipboardUnavailable, match="没能恢复"):
        _enter(ready, "你好")

    assert ready.desktop.characters == []
    assert ready.desktop.pasted == []


def test_逐字符注入也失败时报错_剪贴板已恢复() -> None:
    original = object()
    ready = _scoped()
    ready.desktop.clipboard = original
    ready.desktop.paste_fails = True
    ready.desktop.unicode_fails = True

    with pytest.raises(InjectionError):
        _enter(ready, "你好")

    assert ready.desktop.clipboard is original
    assert ready.desktop.characters == []


def test_尚未声明任务作用域时不输入() -> None:
    desktop = FakeDesktop([window(handle=1)])
    desktop.clipboard = "用户复制的"
    screenshots = Screenshots()

    with pytest.raises(Intercepted, match="尚未声明任务作用域"):
        type_text(
            desktop,
            screenshots,
            TaskScope(),
            _observed(desktop, screenshots, 1),
            "你好",
            intent="填写姓名",
            dangerous=False,
        )

    assert desktop.trace == []
    assert desktop.clipboard == "用户复制的"


def test_目标窗口不在任务作用域内时不输入() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="无标题 - 记事本"), window(handle=2, title="计算器")]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [2])

    with pytest.raises(Intercepted, match="任务作用域之外"):
        type_text(
            desktop,
            screenshots,
            scope,
            _observed(desktop, screenshots, 1),
            "你好",
            intent="填写姓名",
            dangerous=False,
        )

    assert desktop.trace == []


def test_认不出进程的窗口即使在作用域内也不输入() -> None:
    desktop = FakeDesktop([window(handle=1, title="提权", process_name="")])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id = _observed(desktop, screenshots, 1)
    arguments = {
        "screenshot_id": screenshot_id,
        "text": "你好",
        "intent": "填写姓名",
        "dangerous": True,
    }
    decide(desktop, {"tool_name": "mcp__computer-use__type_text", "tool_input": arguments})

    with pytest.raises(Intercepted, match="无法确认"):
        type_text(
            desktop,
            screenshots,
            scope,
            screenshot_id,
            "你好",
            intent="填写姓名",
            dangerous=True,
        )

    assert desktop.trace == []


def test_高危窗口即使在作用域内也须经人确认才输入() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="管理员: Windows PowerShell", process_name="powershell.exe")]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id = _observed(desktop, screenshots, 1)
    arguments = {
        "screenshot_id": screenshot_id,
        "text": "你好",
        "intent": "填写姓名",
        "dangerous": True,
    }

    with pytest.raises(Intercepted, match="高危窗口") as intercepted:
        type_text(
            desktop,
            screenshots,
            scope,
            screenshot_id,
            "你好",
            intent="填写姓名",
            dangerous=False,
        )

    assert "dangerous" in str(intercepted.value)
    assert desktop.trace == []

    with pytest.raises(Intercepted, match="没有经过人的裁决"):
        type_text(
            desktop,
            screenshots,
            scope,
            screenshot_id,
            "你好",
            intent="填写姓名",
            dangerous=True,
        )

    assert desktop.trace == []
    decision = decide(
        desktop, {"tool_name": "mcp__computer-use__type_text", "tool_input": arguments}
    )
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"

    type_text(
        desktop,
        screenshots,
        scope,
        screenshot_id,
        "你好",
        intent="填写姓名",
        dangerous=True,
    )

    assert desktop.pasted == ["你好"]


def test_作用域内窗口弹出的对话框可以输入() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="另存为", owner=1, process_id=1, rect=Rect(100, 100, 400, 300)),
            window(handle=1, title="无标题 - 记事本"),
        ]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    result = type_text(
        desktop,
        screenshots,
        scope,
        _observed(desktop, screenshots, 2),
        "报告.docx",
        intent="填写文件名",
        dangerous=False,
    )

    assert result["window"]["title"] == "<untrusted-screen>另存为</untrusted-screen>"
    assert desktop.pasted == ["报告.docx"]
    assert desktop.trace[0] == ("focus", 2)


def test_窗口已经关掉时不输入() -> None:
    screenshots, scope = Screenshots(), TaskScope()
    before = FakeDesktop([window(handle=1, title="无标题 - 记事本")])
    declare_scope(before, scope, [1])
    screenshot_id = _observed(before, screenshots, 1)
    after = FakeDesktop([])

    with pytest.raises(Intercepted, match="已经不在了"):
        type_text(after, screenshots, scope, screenshot_id, "你好", intent="填写姓名", dangerous=False)

    assert after.trace == []


def test_句柄被别的进程复用后不再输入() -> None:
    screenshots, scope = Screenshots(), TaskScope()
    before = FakeDesktop([window(handle=1, process_id=100)])
    declare_scope(before, scope, [1])
    screenshot_id = _observed(before, screenshots, 1)
    after = FakeDesktop([window(handle=1, process_id=200, title="换了主人")])

    with pytest.raises(Intercepted, match="任务作用域之外"):
        type_text(after, screenshots, scope, screenshot_id, "你好", intent="填写姓名", dangerous=False)

    assert after.trace == []


def test_未知的截图不输入_剪贴板不动() -> None:
    ready = _scoped()
    ready.desktop.clipboard = "用户复制的"

    with pytest.raises(ObservationError):
        type_text(
            ready.desktop,
            Screenshots(),
            TaskScope(),
            "shot-不存在",
            "你好",
            intent="填写姓名",
            dangerous=False,
        )

    assert ready.desktop.trace == []
    assert ready.desktop.clipboard == "用户复制的"


def test_自报危险的文本输入未经裁决不注入_经_hook_交人后才输入() -> None:
    ready = _scoped()
    screenshot_id = _observed(ready.desktop, ready.screenshots, 1)
    arguments = {
        "screenshot_id": screenshot_id,
        "text": "你好",
        "intent": "填写姓名",
        "dangerous": True,
    }

    with pytest.raises(Intercepted, match="没有经过人的裁决"):
        type_text(
            ready.desktop,
            ready.screenshots,
            ready.scope,
            screenshot_id,
            "你好",
            intent="填写姓名",
            dangerous=True,
        )

    assert ready.desktop.trace == []
    decision = decide(
        ready.desktop, {"tool_name": "mcp__computer-use__type_text", "tool_input": arguments}
    )
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"

    result = type_text(
        ready.desktop,
        ready.screenshots,
        ready.scope,
        screenshot_id,
        "你好",
        intent="填写姓名",
        dangerous=True,
    )

    assert result["tier"] == "clipboard"
    assert ready.desktop.pasted == ["你好"]


def test_文本输入可经由_MCP_调用_日志不记下文本本身() -> None:
    desktop = FakeDesktop([window(handle=1, title="无标题 - 记事本")])
    desktop.clipboard = "用户复制的"

    result = asyncio.run(_type_via_mcp(desktop))

    assert result == {
        "window": {
            "handle": 1,
            "title": "<untrusted-screen>无标题 - 记事本</untrusted-screen>",
            "process_name": "notepad.exe",
        },
        "tier": "clipboard",
        "clipboard_used": True,
    }
    assert desktop.pasted == ["你好"]
    assert desktop.clipboard == "用户复制的"
    recorded = [entry for entry in desktop.action_log() if entry["tool"] == "type_text"]
    assert [(entry["intent"], entry["outcome"], entry["dangerous"]) for entry in recorded] == [
        ("填写姓名", "succeeded", False)
    ]
    assert "你好" not in json.dumps(recorded[0]["target"], ensure_ascii=False)


async def _type_via_mcp(desktop: FakeDesktop) -> dict[str, Any]:
    async with Client(create_server(desktop)) as client:
        await client.call_tool("declare_scope", {"handles": [1]})
        observed = await client.call_tool("observe_window", {"handle": 1})
        typed = await client.call_tool(
            "type_text",
            {
                "screenshot_id": observed.data["screenshot_id"],
                "text": "你好",
                "intent": "填写姓名",
                "dangerous": False,
            },
        )
        return dict(typed.data)


def test_作用域外的文本输入经由_MCP_返回错误且不注入() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="无标题 - 记事本"), window(handle=2, title="计算器")]
    )

    async def call() -> None:
        async with Client(create_server(desktop)) as client:
            await client.call_tool("declare_scope", {"handles": [2]})
            observed = await client.call_tool("observe_window", {"handle": 1})
            with pytest.raises(ToolError, match="任务作用域之外"):
                await client.call_tool(
                    "type_text",
                    {
                        "screenshot_id": observed.data["screenshot_id"],
                        "text": "不该打出去",
                        "intent": "填写姓名",
                        "dangerous": False,
                    },
                )

    asyncio.run(call())

    assert desktop.pasted == []
    assert desktop.characters == []


def test_逐字符注入按码位送入_不把增补平面拆开() -> None:
    ready = _scoped()
    ready.desktop.paste_fails = True

    _enter(ready, "A😀")

    assert ready.desktop.characters == ["A", "😀"]


@dataclass
class _Ready:
    desktop: FakeDesktop
    screenshots: Screenshots
    scope: TaskScope


def _scoped() -> _Ready:
    desktop = FakeDesktop([window(handle=1, title="无标题 - 记事本")])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    return _Ready(desktop, screenshots, scope)


def _enter(ready: _Ready, text: str, *, dangerous: bool = False) -> dict[str, Any]:
    return type_text(
        ready.desktop,
        ready.screenshots,
        ready.scope,
        _observed(ready.desktop, ready.screenshots, 1),
        text,
        intent="填写姓名",
        dangerous=dangerous,
    )
