"""PreToolUse hook 薄壳：从标准输入读 Claude Code 的调用信息，判为危险时交给人裁决。

判定用的是服务端同一个 `judge_call`，连续输入已达预算时也会交给人。
hook 看不到截图，落点附近的文字由服务端自己再看一眼。
模型自报不危险而读到高危词时，服务端在同一次调用里弹原生对话框问人，不要求模型改标志重来。
急停之后的输入工具不在这里交人：服务端会直接拒绝，交给人也执行不了。
"""

from __future__ import annotations

import io
import json
import sys
from typing import Any, Mapping, TextIO

from computer_use.danger import INPUT_TOOLS, Verdict, judge_call
from computer_use.desktop import DesktopPort
from computer_use.files import describe_change
from computer_use.interception import refer_to_human
from computer_use.pace import confirmation_reason
from computer_use.powershell import describe_command


def decide(desktop: DesktopPort, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """对一次调用的裁决：判为危险时交给人（`ask`），否则为 `None`，按 Claude Code 平常的权限流程走。"""

    tool_name = str(payload.get("tool_name", ""))
    if not tool_name.startswith("mcp__"):
        return None
    tool = tool_name.split("__", 2)[-1]
    arguments = payload.get("tool_input")
    if not isinstance(arguments, Mapping):
        arguments = {}
    if tool in INPUT_TOOLS and desktop.read_pace().stopped:
        return None
    verdict = judge_call(tool, arguments)
    if (reason := confirmation_reason(desktop, tool)) is not None:
        verdict = verdict | Verdict((reason,))
    if not verdict.dangerous:
        return None
    refer_to_human(desktop, tool, arguments)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                f"computer-use 请你裁决一次危险动作（{'；'.join(verdict.reasons)}）。"
                f"{_describe(desktop, tool, arguments)}"
            ),
        }
    }


def run(desktop: DesktopPort, stdin: TextIO, stdout: TextIO) -> None:
    output = decide(desktop, json.load(stdin))
    if output is not None:
        json.dump(output, stdout)


def main() -> None:
    from computer_use.win32_desktop import Win32Desktop

    run(Win32Desktop(), io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"), sys.stdout)


def _describe(desktop: DesktopPort, tool: str, arguments: Mapping[str, Any]) -> str:
    intent = arguments.get("intent")
    point = {
        "click": "单击",
        "double_click": "双击",
        "right_click": "右键单击",
        "scroll": "滚动",
    }
    if tool in point:
        target = (
            f"在截图 {arguments.get('screenshot_id')} 的 "
            f"({arguments.get('x')}, {arguments.get('y')}) 处{point[tool]}"
        )
    elif tool == "drag":
        target = (
            f"在截图 {arguments.get('screenshot_id')} 上从 "
            f"({arguments.get('x')}, {arguments.get('y')}) 拖到 "
            f"({arguments.get('to_x')}, {arguments.get('to_y')})"
        )
    elif tool == "press_keys":
        keys = arguments.get("keys")
        chord = "+".join(str(key) for key in keys) if isinstance(keys, list) else str(keys)
        target = f"在截图 {arguments.get('screenshot_id')} 的窗口按下 {chord}"
    elif tool == "launch_app":
        target = f"启动 {arguments.get('app')}"
    elif (change := describe_change(desktop, tool, arguments)) is not None:
        target = change
    elif (command := describe_command(tool, arguments)) is not None:
        target = command
    else:
        target = f"调用 {tool}，参数 {json.dumps(dict(arguments), ensure_ascii=False)}"
    return f"模型自述意图：「{intent}」；{target}。"


if __name__ == "__main__":
    main()
