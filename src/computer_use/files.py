"""核心：文件读写。读取自由放行；写入、移动、删除须经与桌面动作同一套裁决。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

from typing import Any, Mapping

from computer_use.danger import judge_call
from computer_use.desktop import DesktopPort
from computer_use.interception import require_ruling


def describe_change(desktop: DesktopPort, tool: str, arguments: Mapping[str, Any]) -> str | None:
    """给裁决人看的变更说明：目标路径与变更类型。覆盖既有文件时类型为「覆盖」。"""

    if tool == "write_file":
        path = str(arguments.get("path"))
        kind = "覆盖" if desktop.path_kind(path) == "file" else "写入"
        return f"变更类型：{kind}；目标路径：{path}"
    if tool == "move_file":
        source = str(arguments.get("source"))
        destination = str(arguments.get("destination"))
        kind = "覆盖" if desktop.path_kind(destination) == "file" else "移动"
        return f"变更类型：{kind}；从 {source} 到 {destination}"
    if tool == "delete_file":
        path = str(arguments.get("path"))
        kind = "永久删除" if arguments.get("permanent") is True else "移入回收站"
        return f"变更类型：{kind}；目标路径：{path}"
    return None


def write_file(
    desktop: DesktopPort,
    path: str,
    content: str,
    *,
    intent: str,
    dangerous: bool,
) -> dict[str, Any]:
    """把文本写入 `path`。未经人裁决时抛 `Intercepted`，文件保持原样。"""

    arguments = {"path": path, "content": content, "intent": intent, "dangerous": dangerous}
    require_ruling(desktop, "write_file", arguments, judge_call("write_file", arguments))
    desktop.write_text(path, content)
    return {"path": path}


def move_file(
    desktop: DesktopPort,
    source: str,
    destination: str,
    *,
    intent: str,
    dangerous: bool,
) -> dict[str, Any]:
    """把文件或目录挪到新路径。未经人裁决时抛 `Intercepted`，两边都保持原样。"""

    arguments = {
        "source": source,
        "destination": destination,
        "intent": intent,
        "dangerous": dangerous,
    }
    require_ruling(desktop, "move_file", arguments, judge_call("move_file", arguments))
    desktop.move_path(source, destination)
    return {"source": source, "destination": destination}


def delete_file(
    desktop: DesktopPort,
    path: str,
    *,
    intent: str,
    dangerous: bool,
    permanent: bool | None = None,
) -> dict[str, Any]:
    """删除路径。默认移入回收站。`permanent` 为真才永久删除，且须单独裁决。

    未经人裁决时抛 `Intercepted`，文件保持原样。`permanent` 省略时不写进调用参数，
    以免和模型实际发出的参数对不上。
    """

    arguments: dict[str, Any] = {"path": path, "intent": intent, "dangerous": dangerous}
    if permanent is not None:
        arguments["permanent"] = permanent
    require_ruling(desktop, "delete_file", arguments, judge_call("delete_file", arguments))
    desktop.delete_path(path, permanent=permanent is True)
    return {"path": path, "permanent": permanent is True}
