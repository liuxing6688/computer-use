"""PowerShell 命令解析：整条命令都要看，伪装成只读的写操作也要被认出来。

判定是纯函数。执行走 `DesktopPort`，只读放行，含写操作的命令未经裁决不执行。
"""

from __future__ import annotations

from typing import Any

import pytest

from computer_use.action_log import Intercepted
from computer_use.danger import judge_call
from computer_use.desktop import CommandResult
from computer_use.hook import decide
from computer_use.powershell import judge_command
from computer_use.tools import run_powershell

from .fake_desktop import FakeDesktop


def test_管道末尾的写操作不会因为开头是只读就被放行() -> None:
    assert not judge_command("Get-ChildItem").dangerous

    verdict = judge_command("Get-ChildItem | Remove-Item")

    assert verdict.dangerous
    assert any("写" in reason for reason in verdict.reasons)


def test_分号后面的写操作也要认出来() -> None:
    verdict = judge_command("Get-ChildItem; Remove-Item foo")

    assert verdict.dangerous
    assert any("写" in reason for reason in verdict.reasons)


def test_脚本块里的写操作也要认出来() -> None:
    verdict = judge_command("Get-ChildItem | ForEach-Object { Remove-Item $_.FullName }")

    assert verdict.dangerous
    assert any("写" in reason for reason in verdict.reasons)


def test_子表达式里的写操作也要认出来() -> None:
    verdict = judge_command("Get-Content $(Remove-Item foo)")

    assert verdict.dangerous
    assert any("写" in reason for reason in verdict.reasons)


def test_比较运算符不是命令() -> None:
    assert not judge_command("Get-ChildItem | Where-Object { $_.Name -eq 'a' }").dangerous


def test_字符串和注释里的写动词不算写操作() -> None:
    assert not judge_command("Get-Content 'Remove-Item'").dangerous
    assert not judge_command('Get-Content "Remove-Item"').dangerous
    assert not judge_command("Get-Date # Remove-Item later").dangerous


def test_重定向到文件是写操作_合并错误流不是() -> None:
    assert judge_command("Get-Content a.txt > b.txt").dangerous
    assert not judge_command("Get-Process 2>&1").dangerous


def test_对象方法上的下载也判为危险() -> None:
    verdict = judge_command("$client.DownloadFile('https://example.com/a', 'a.bin')")

    assert verdict.dangerous
    assert any("下载" in reason for reason in verdict.reasons)


@pytest.mark.parametrize(
    "command",
    [
        "Invoke-WebRequest https://example.com",
        "curl https://example.com",
        "iwr https://example.com -OutFile page.html",
        "Invoke-RestMethod https://example.com/api",
    ],
)
def test_下载类调用一律判为危险(command: str) -> None:
    verdict = judge_command(command)

    assert verdict.dangerous
    assert any("下载" in reason for reason in verdict.reasons)


@pytest.mark.parametrize(
    "command",
    [
        'Invoke-Expression "Get-Date"',
        "iex 'Get-Date'",
        "& $cmd",
        ". .\\script.ps1",
        "[scriptblock]::Create('Get-Date')",
    ],
)
def test_动态求值一律判为危险(command: str) -> None:
    verdict = judge_command(command)

    assert verdict.dangerous
    assert any("动态求值" in reason for reason in verdict.reasons)


def test_别名与大小写藏不住写操作() -> None:
    assert judge_command("dir | del").dangerous
    assert judge_command("GET-CHILDITEM | rEmOvE-iTeM").dangerous


def test_引号没闭合时无从确认只读() -> None:
    verdict = judge_command("Get-Content 'half")

    assert verdict.dangerous
    assert any("无法解析" in reason for reason in verdict.reasons)


def test_不在只读白名单的命令不放行() -> None:
    verdict = judge_command("Get-ChildItem | Mystery-Cmdlet")

    assert verdict.dangerous
    assert any("白名单" in reason for reason in verdict.reasons)


def test_只读命令不经确认直接返回输出() -> None:
    desktop = FakeDesktop()
    desktop.command_result = CommandResult(stdout="a.txt\n", stderr="", exit_code=0)
    arguments: dict[str, Any] = {
        "command": "Get-ChildItem",
        "intent": "看看目录",
        "dangerous": False,
    }

    assert decide(desktop, _payload(arguments)) is None
    assert run_powershell(desktop, **arguments) == {
        "stdout": "a.txt\n",
        "stderr": "",
        "exit_code": 0,
    }
    assert desktop.commands == ["Get-ChildItem"]


def test_未经裁决的写命令不会启动() -> None:
    desktop = FakeDesktop()
    arguments: dict[str, Any] = {
        "command": "Get-ChildItem | Remove-Item",
        "intent": "清空目录",
        "dangerous": False,
    }

    output = decide(desktop, _payload(arguments))

    assert output is not None
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "写" in reason
    assert "Get-ChildItem | Remove-Item" in reason

    untouched = FakeDesktop()
    with pytest.raises(Intercepted):
        run_powershell(untouched, **arguments)
    assert untouched.commands == []


def test_人裁决之后写命令才会执行() -> None:
    desktop = FakeDesktop()
    desktop.command_result = CommandResult(stdout="", stderr="", exit_code=0)
    arguments: dict[str, Any] = {
        "command": "Remove-Item foo.txt",
        "intent": "删掉草稿",
        "dangerous": True,
    }

    decide(desktop, _payload(arguments))
    assert run_powershell(desktop, **arguments)["exit_code"] == 0
    assert desktop.commands == ["Remove-Item foo.txt"]


def test_hook与服务端对同一条命令判定一致() -> None:
    command = "Get-Date; Invoke-Expression 'Remove-Item foo'"
    arguments = {"command": command, "intent": "跑一下", "dangerous": False}

    verdict = judge_call("run_powershell", arguments)

    assert verdict.dangerous
    assert any("动态求值" in reason for reason in verdict.reasons)


def _payload(tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": "session-1",
        "transcript_path": "C:/transcript.jsonl",
        "cwd": "E:/",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__computer-use__run_powershell",
        "tool_input": tool_input,
        "tool_use_id": "toolu_01",
    }
