"""文件工具：读取与列目录自由放行，写入、移动、删除走同一套拦截与日志。

文件系统活在 `DesktopPort` 里。测试替身持有一份内存文件系统，无需碰盘。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from computer_use.action_log import Intercepted
from computer_use.desktop import FileError
from computer_use.hook import decide
from computer_use.server import create_server
from computer_use.tools import delete_file, list_directory, move_file, read_file, write_file

from .fake_desktop import FakeDesktop


def test_读取文件与列出目录无需确认() -> None:
    desktop = FakeDesktop(
        files={
            "E:/notes/a.txt": "hello",
            "E:/notes/sub": None,
        }
    )

    assert read_file(desktop, "E:/notes/a.txt") == {
        "path": "E:/notes/a.txt",
        "content": "hello",
    }
    assert list_directory(desktop, "E:/notes") == [
        {"name": "a.txt", "is_dir": False},
        {"name": "sub", "is_dir": True},
    ]
    assert desktop.tickets == {}
    assert desktop.dialogs == []


def test_新建文件的确认信息写明写入而不是覆盖() -> None:
    desktop = FakeDesktop(files={"E:/notes": None})
    arguments: dict[str, Any] = {
        "path": "E:/notes/a.txt",
        "content": "hello",
        "intent": "记下这句话",
        "dangerous": False,
    }

    output = decide(desktop, _payload(arguments))

    assert output is not None
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "E:/notes/a.txt" in reason
    assert "写入" in reason
    assert "覆盖" not in reason
    write_file(desktop, **arguments)
    assert read_file(desktop, "E:/notes/a.txt")["content"] == "hello"


def test_未经裁决的写入不落盘() -> None:
    desktop = FakeDesktop(files={"E:/notes": None})

    with pytest.raises(Intercepted):
        write_file(desktop, "E:/notes/a.txt", "hello", intent="记下这句话", dangerous=True)

    with pytest.raises(FileError):
        read_file(desktop, "E:/notes/a.txt")


def test_覆盖既有文件时确认信息写明覆盖_允许后才替换内容() -> None:
    desktop = FakeDesktop(files={"E:/notes/a.txt": "old"})
    arguments: dict[str, Any] = {
        "path": "E:/notes/a.txt",
        "content": "new",
        "intent": "改一句",
        "dangerous": False,
    }

    output = decide(desktop, _payload(arguments))

    assert output is not None
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "E:/notes/a.txt" in reason
    assert "覆盖" in reason
    write_file(desktop, **arguments)
    assert read_file(desktop, "E:/notes/a.txt")["content"] == "new"


def test_未经裁决的移动不发生() -> None:
    desktop = FakeDesktop(files={"E:/notes/a.txt": "hello", "E:/other": None})

    with pytest.raises(Intercepted):
        move_file(
            desktop, "E:/notes/a.txt", "E:/other/b.txt",
            intent="归档", dangerous=True,
        )

    assert read_file(desktop, "E:/notes/a.txt")["content"] == "hello"
    with pytest.raises(FileError):
        read_file(desktop, "E:/other/b.txt")


def test_移动的确认信息含两端路径_允许后文件才到新路径() -> None:
    desktop = FakeDesktop(files={"E:/notes/a.txt": "hello", "E:/other": None})
    arguments: dict[str, Any] = {
        "source": "E:/notes/a.txt",
        "destination": "E:/other/b.txt",
        "intent": "归档",
        "dangerous": True,
    }

    output = decide(desktop, _payload(arguments, tool="move_file"))

    assert output is not None
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "E:/notes/a.txt" in reason and "E:/other/b.txt" in reason
    assert "移动" in reason
    move_file(desktop, **arguments)
    with pytest.raises(FileError):
        read_file(desktop, "E:/notes/a.txt")
    assert read_file(desktop, "E:/other/b.txt")["content"] == "hello"


def test_移动到已有文件时确认信息写明覆盖() -> None:
    desktop = FakeDesktop(files={"E:/a.txt": "one", "E:/b.txt": "two"})
    arguments: dict[str, Any] = {
        "source": "E:/a.txt",
        "destination": "E:/b.txt",
        "intent": "换成这一份",
        "dangerous": True,
    }

    output = decide(desktop, _payload(arguments, tool="move_file"))

    assert output is not None
    assert "覆盖" in output["hookSpecificOutput"]["permissionDecisionReason"]
    move_file(desktop, **arguments)
    assert read_file(desktop, "E:/b.txt")["content"] == "one"
    with pytest.raises(FileError):
        read_file(desktop, "E:/a.txt")


def test_未经裁决的删除不发生() -> None:
    desktop = FakeDesktop(files={"E:/notes/a.txt": "hello"})

    with pytest.raises(Intercepted):
        delete_file(desktop, "E:/notes/a.txt", intent="清掉草稿", dangerous=True)

    assert read_file(desktop, "E:/notes/a.txt")["content"] == "hello"
    assert desktop.recycled == []


def test_删除默认移入回收站且不弹原生对话框() -> None:
    desktop = FakeDesktop(files={"E:/notes/a.txt": "hello"})
    arguments = {"path": "E:/notes/a.txt", "intent": "清掉草稿", "dangerous": False}
    output = decide(desktop, _payload(arguments, tool="delete_file"))

    assert output is not None
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "E:/notes/a.txt" in reason
    assert "移入回收站" in reason
    assert "永久" not in reason
    delete_file(desktop, "E:/notes/a.txt", intent="清掉草稿", dangerous=False)
    with pytest.raises(FileError):
        read_file(desktop, "E:/notes/a.txt")
    assert desktop.recycled == ["E:/notes/a.txt"]
    assert desktop.deleted == []
    assert desktop.dialogs == []


def test_移入回收站的裁决不能改成永久删除() -> None:
    desktop = FakeDesktop(files={"E:/notes/a.txt": "hello"})
    decide(
        desktop,
        _payload(
            {"path": "E:/notes/a.txt", "intent": "清掉草稿", "dangerous": True},
            tool="delete_file",
        ),
    )

    with pytest.raises(Intercepted):
        delete_file(
            desktop, "E:/notes/a.txt", intent="清掉草稿", dangerous=True, permanent=True
        )

    assert read_file(desktop, "E:/notes/a.txt")["content"] == "hello"
    assert desktop.deleted == []


def test_永久删除被拒绝时文件还在_确认写明路径且不进回收站() -> None:
    desktop = FakeDesktop(files={"E:/notes/a.txt": "hello"})
    desktop.dialog_reply = False
    arguments: dict[str, Any] = {
        "path": "E:/notes/a.txt",
        "permanent": True,
        "intent": "抹掉这份草稿",
        "dangerous": True,
    }
    decide(desktop, _payload(arguments, tool="delete_file"))

    with pytest.raises(Intercepted, match="拒绝"):
        delete_file(desktop, **arguments)

    assert read_file(desktop, "E:/notes/a.txt")["content"] == "hello"
    assert desktop.recycled == []
    assert desktop.deleted == []
    [dialog] = desktop.dialogs
    assert "E:/notes/a.txt" in dialog.message
    assert "永久删除" in dialog.message


def test_永久删除超时按拒绝处理_文件还在() -> None:
    desktop = FakeDesktop(files={"E:/notes/a.txt": "hello"})
    desktop.dialog_reply = None
    arguments: dict[str, Any] = {
        "path": "E:/notes/a.txt",
        "permanent": True,
        "intent": "抹掉这份草稿",
        "dangerous": True,
    }
    decide(desktop, _payload(arguments, tool="delete_file"))

    with pytest.raises(Intercepted, match="超时"):
        delete_file(desktop, **arguments)

    assert read_file(desktop, "E:/notes/a.txt")["content"] == "hello"
    assert desktop.deleted == []


def test_文件工具经由_MCP_共用拦截与日志_内容不写入日志() -> None:
    desktop = FakeDesktop(files={"E:/notes/a.txt": "old", "E:/notes/sub": None})
    request = {
        "path": "E:/notes/a.txt",
        "content": "new",
        "intent": "改一句",
        "dangerous": True,
    }

    async def call() -> tuple[Any, Any]:
        async with Client(create_server(desktop)) as client:
            listed = await client.call_tool("list_directory", {"path": "E:/notes"})
            read = await client.call_tool("read_file", {"path": "E:/notes/a.txt"})
            with pytest.raises(ToolError):
                await client.call_tool("write_file", request)
            decide(desktop, _payload(request))
            await client.call_tool("write_file", request)
            return listed.data, read.data

    listed, read = asyncio.run(call())

    assert read == {"path": "E:/notes/a.txt", "content": "old"}
    assert {item["name"] for item in listed} == {"a.txt", "sub"}
    assert read_file(desktop, "E:/notes/a.txt")["content"] == "new"
    listed_record, read_record, refused, written = desktop.action_log()
    assert (listed_record["tool"], listed_record["outcome"]) == ("list_directory", "succeeded")
    assert (read_record["tool"], read_record["outcome"]) == ("read_file", "succeeded")
    assert (refused["verdict"], refused["outcome"]) == ("intercepted", "not_executed")
    assert written["target"] == {"path": "E:/notes/a.txt"}
    assert written["intent"] == "改一句"
    assert (written["verdict"], written["outcome"]) == ("allowed", "succeeded")
    assert "new" not in json.dumps(written["target"], ensure_ascii=False)


def _payload(tool_input: dict[str, Any], tool: str = "write_file") -> dict[str, Any]:
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
