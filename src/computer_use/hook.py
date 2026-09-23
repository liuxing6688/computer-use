"""PreToolUse hook 薄壳：从标准输入读 Claude Code 的调用信息，判为危险时交给人裁决。

判定用的是服务端同一个 `judge_call`。hook 看不到截图，落点附近的文字由服务端自己再看一眼；
hook 放行而服务端判为危险的调用，会被服务端拦下并要求模型自报后重来，那时再经过这里。
"""

from __future__ import annotations

import io
import json
import sys
from typing import Any, Mapping, TextIO

from computer_use.danger import judge_call
from computer_use.desktop import DesktopPort
from computer_use.interception import refer_to_human


def decide(desktop: DesktopPort, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """对一次调用的裁决：判为危险时交给人（`ask`），否则为 `None`，按 Claude Code 平常的权限流程走。"""

    tool_name = str(payload.get("tool_name", ""))
    if not tool_name.startswith("mcp__"):
        return None
    tool = tool_name.split("__", 2)[-1]
    arguments = payload.get("tool_input")
    if not isinstance(arguments, Mapping):
        arguments = {}
    verdict = judge_call(tool, arguments)
    if not verdict.dangerous:
        return None
    refer_to_human(desktop, tool, arguments)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                f"computer-use 请你裁决一次危险动作（{'；'.join(verdict.reasons)}）。"
                f"{_describe(tool, arguments)}"
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


def _describe(tool: str, arguments: Mapping[str, Any]) -> str:
    intent = arguments.get("intent")
    if tool == "click":
        target = (
            f"在截图 {arguments.get('screenshot_id')} 的 "
            f"({arguments.get('x')}, {arguments.get('y')}) 处单击"
        )
    else:
        target = f"调用 {tool}，参数 {json.dumps(dict(arguments), ensure_ascii=False)}"
    return f"模型自述意图：「{intent}」；{target}。"


if __name__ == "__main__":
    main()
