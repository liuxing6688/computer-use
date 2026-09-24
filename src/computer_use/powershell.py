"""核心：PowerShell 整条命令的判定与执行。

只读命令自由放行。写操作、下载与动态求值一律危险，管道、分号、脚本块和子表达式里的也算。
字符串和注释里的词不算调用。解析不了就判为危险：看不懂就不放行。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from computer_use.danger import Verdict, judge_call
from computer_use.desktop import DesktopPort
from computer_use.interception import require_ruling

_READ = frozenset(
    {
        "get-childitem",
        "get-content",
        "get-item",
        "get-itemproperty",
        "get-itempropertyvalue",
        "get-location",
        "get-process",
        "get-service",
        "get-date",
        "get-host",
        "get-history",
        "get-help",
        "get-command",
        "get-member",
        "get-alias",
        "get-variable",
        "get-culture",
        "get-uiculture",
        "get-psdrive",
        "get-psprovider",
        "get-module",
        "get-verb",
        "get-winevent",
        "get-eventlog",
        "get-computerinfo",
        "get-ciminstance",
        "get-wmiobject",
        "get-localuser",
        "get-localgroup",
        "get-localgroupmember",
        "get-netipaddress",
        "get-netipconfiguration",
        "get-netadapter",
        "get-nettcpconnection",
        "get-disk",
        "get-volume",
        "get-partition",
        "get-physicaldisk",
        "get-clipboard",
        "get-timezone",
        "get-executionpolicy",
        "get-acl",
        "get-authenticodesignature",
        "get-filehash",
        "get-random",
        "select-object",
        "select-string",
        "select-xml",
        "where-object",
        "sort-object",
        "group-object",
        "measure-object",
        "measure-command",
        "compare-object",
        "format-table",
        "format-list",
        "format-wide",
        "format-custom",
        "format-hex",
        "out-string",
        "out-host",
        "out-null",
        "out-default",
        "write-output",
        "write-host",
        "write-debug",
        "write-verbose",
        "write-warning",
        "write-information",
        "write-error",
        "write-progress",
        "convertto-json",
        "convertfrom-json",
        "convertto-csv",
        "convertfrom-csv",
        "convertto-html",
        "convertto-xml",
        "convertfrom-stringdata",
        "join-path",
        "split-path",
        "resolve-path",
        "test-path",
        "convert-path",
        "set-location",
        "push-location",
        "pop-location",
        "start-sleep",
        "foreach-object",
        "import-csv",
        "add-member",
        "clear-host",
    }
)
"""已知只读（或只改会话位置、不落盘）的命令。别名先折成这些名字。"""

_DOWNLOAD = frozenset(
    {
        "invoke-webrequest",
        "invoke-restmethod",
        "start-bitstransfer",
        "curl.exe",
        "wget.exe",
    }
)
_DOWNLOAD_METHODS = frozenset(
    {
        "downloadfile",
        "downloadstring",
        "downloaddata",
        "downloadfileasync",
        "uploadfile",
        "uploadstring",
        "uploaddata",
        "uploadvalues",
    }
)
_DYNAMIC = frozenset(
    {
        "invoke-expression",
        "invoke-command",
        "add-type",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "cmd",
        "cmd.exe",
    }
)
_WRITE_VERBS = frozenset(
    {
        "add",
        "clear",
        "copy",
        "move",
        "new",
        "remove",
        "rename",
        "set",
        "stop",
        "start",
        "restart",
        "export",
        "import",
        "register",
        "unregister",
        "enable",
        "disable",
        "install",
        "uninstall",
        "update",
        "save",
        "publish",
        "send",
        "out",
        "tee",
        "invoke",
    }
)
_ALIASES = {
    "cat": "get-content",
    "gc": "get-content",
    "type": "get-content",
    "ls": "get-childitem",
    "dir": "get-childitem",
    "gci": "get-childitem",
    "ps": "get-process",
    "gps": "get-process",
    "gi": "get-item",
    "gp": "get-itemproperty",
    "gl": "get-location",
    "pwd": "get-location",
    "gsv": "get-service",
    "gm": "get-member",
    "gcm": "get-command",
    "gal": "get-alias",
    "gv": "get-variable",
    "select": "select-object",
    "sls": "select-string",
    "where": "where-object",
    "?": "where-object",
    "foreach": "foreach-object",
    "%": "foreach-object",
    "sort": "sort-object",
    "group": "group-object",
    "measure": "measure-object",
    "ft": "format-table",
    "fl": "format-list",
    "fw": "format-wide",
    "echo": "write-output",
    "write": "write-output",
    "man": "get-help",
    "help": "get-help",
    "cd": "set-location",
    "chdir": "set-location",
    "sl": "set-location",
    "cls": "clear-host",
    "clear": "clear-host",
    "ri": "remove-item",
    "rm": "remove-item",
    "rmdir": "remove-item",
    "del": "remove-item",
    "erase": "remove-item",
    "rd": "remove-item",
    "mi": "move-item",
    "move": "move-item",
    "mv": "move-item",
    "ni": "new-item",
    "rni": "rename-item",
    "ren": "rename-item",
    "copy": "copy-item",
    "cp": "copy-item",
    "cpi": "copy-item",
    "sc": "set-content",
    "ac": "add-content",
    "clc": "clear-content",
    "si": "set-item",
    "sp": "set-itemproperty",
    "iwr": "invoke-webrequest",
    "wget": "invoke-webrequest",
    "curl": "invoke-webrequest",
    "irm": "invoke-restmethod",
    "iex": "invoke-expression",
    "icm": "invoke-command",
    "tee": "tee-object",
    "epcsv": "export-csv",
    "ipcsv": "import-csv",
    "oh": "out-host",
    "start": "start-process",
    "saps": "start-process",
    "kill": "stop-process",
    "spps": "stop-process",
    "ii": "invoke-item",
    "sal": "set-alias",
}
_KEYWORDS = frozenset(
    {
        "if",
        "else",
        "elseif",
        "for",
        "while",
        "do",
        "until",
        "switch",
        "try",
        "catch",
        "finally",
        "param",
        "return",
        "break",
        "continue",
        "throw",
        "exit",
        "begin",
        "process",
        "end",
        "in",
        "data",
        "using",
        "dynamicparam",
        "else",
    }
)
_NAME_KEYWORDS = frozenset({"function", "filter", "class", "enum", "trap"})

_WRITE = "命令含写操作"
_FETCH = "命令含下载"
_EVAL = "命令含动态求值"
_UNKNOWN = "命令不在只读白名单"
_UNPARSED = "无法解析整条命令，无从确认只读"

_REDIR = re.compile(
    r"(?:(?P<merge>(?:\d+|\*)>&\d)|(?P<file>(?:\d+|\*)?>>)|(?P<file2>(?:\d+|\*)?>(?!&)))"
)


class _Unparsed(Exception):
    """命令没能按 PowerShell 的结构读完。"""


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str


def judge_command(command: str) -> Verdict:
    """解析整条命令。没有理由即只读，可以放行。"""

    try:
        return Verdict(tuple(_walk(_tokenize(command))))
    except _Unparsed:
        return Verdict((_UNPARSED,))


def describe_command(tool: str, arguments: Mapping[str, Any]) -> str | None:
    """给裁决人看的命令原文。不是这条工具时为 `None`。"""

    if tool != "run_powershell":
        return None
    return f"命令：{arguments.get('command')}"


def run_powershell(
    desktop: DesktopPort,
    command: str,
    *,
    intent: str,
    dangerous: bool,
) -> dict[str, Any]:
    """执行一条 PowerShell 命令。判为危险且未经人裁决时抛 `Intercepted`，命令不会启动。"""

    arguments = {"command": command, "intent": intent, "dangerous": dangerous}
    require_ruling(desktop, "run_powershell", arguments, judge_call("run_powershell", arguments))
    result = desktop.run_powershell(command)
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code}


def _walk(tokens: list[_Token]) -> list[str]:
    reasons: list[str] = []
    _statements(tokens, 0, frozenset(), reasons)
    return reasons


def _statements(
    tokens: list[_Token], index: int, stop: frozenset[str], reasons: list[str]
) -> int:
    while index < len(tokens) and tokens[index].kind not in stop:
        if tokens[index].kind == "sep":
            index += 1
            continue
        index = _statement(tokens, index, stop, reasons)
    return index


def _statement(
    tokens: list[_Token], index: int, stop: frozenset[str], reasons: list[str]
) -> int:
    command_position = True
    while index < len(tokens) and tokens[index].kind not in stop | {"sep"}:
        token = tokens[index]
        if token.kind == "pipe":
            command_position = True
            index += 1
            continue
        if token.kind == "redirect":
            _add(reasons, _WRITE)
            index += 1
            continue
        if token.kind in {"lbrace", "sub", "lparen", "array"}:
            close = "rbrace" if token.kind == "lbrace" else "rparen"
            index = _statements(tokens, index + 1, frozenset({close}), reasons)
            if index >= len(tokens) or tokens[index].kind != close:
                raise _Unparsed
            index += 1
            command_position = False
            continue
        if token.kind == "assign":
            command_position = True
            index += 1
            continue
        if token.kind == "call" or (
            token.kind == "word" and token.text == "." and command_position
        ):
            index = _invoke(tokens, index + 1, reasons)
            command_position = False
            continue
        if token.kind == "word" and command_position and not token.text.startswith("-"):
            index = _word_at_command(tokens, index, reasons)
            command_position = False
            continue
        if token.kind == "var" and command_position:
            command_position = False
            index += 1
            continue
        if token.kind == "member":
            if token.text.lower() in _DOWNLOAD_METHODS:
                _add(reasons, _FETCH)
            index += 1
            continue
        if token.kind == "dynamic":
            _add(reasons, _EVAL)
            index += 1
            continue
        index += 1
    return index


def _word_at_command(tokens: list[_Token], index: int, reasons: list[str]) -> int:
    text = tokens[index].text
    lowered = text.lower()
    if lowered in _NAME_KEYWORDS:
        index += 1
        if index < len(tokens) and tokens[index].kind == "word":
            index += 1
        return index
    if lowered == "foreach":
        nxt = tokens[index + 1] if index + 1 < len(tokens) else None
        if nxt is not None and nxt.kind == "lparen":
            return index + 1
        _classify("foreach-object", reasons)
        return index + 1
    if lowered in _KEYWORDS:
        return index + 1
    _classify(text, reasons)
    return index + 1


def _invoke(tokens: list[_Token], index: int, reasons: list[str]) -> int:
    if index >= len(tokens):
        _add(reasons, _EVAL)
        return index
    token = tokens[index]
    if token.kind == "lbrace":
        return index
    if token.kind == "var" or token.kind in {"sub", "lparen"}:
        _add(reasons, _EVAL)
        return index
    if token.kind == "string":
        if token.text == "" or any(char.isspace() for char in token.text):
            _add(reasons, _EVAL)
        else:
            _classify(token.text, reasons)
        return index + 1
    if token.kind == "word":
        if _is_script_path(token.text):
            _add(reasons, _EVAL)
        else:
            _classify(token.text, reasons)
        return index + 1
    _add(reasons, _EVAL)
    return index


def _classify(raw: str, reasons: list[str]) -> None:
    name = _canonical(raw)
    if name in _READ:
        return
    if name in _DOWNLOAD:
        _add(reasons, _FETCH)
        return
    if name in _DYNAMIC:
        _add(reasons, _EVAL)
        return
    verb = name.split("-", 1)[0]
    if verb in _WRITE_VERBS:
        _add(reasons, _WRITE)
        return
    _add(reasons, _UNKNOWN)


def _canonical(raw: str) -> str:
    name = raw.split("\\")[-1].lower()
    return _ALIASES.get(name, name)


def _is_script_path(text: str) -> bool:
    lowered = text.lower()
    return (
        "\\" in text
        or "/" in text
        or lowered.startswith(".")
        or lowered.endswith((".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".exe"))
    )


def _add(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _tokenize(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in " \t\r":
            index += 1
            continue
        if char == "`":
            index = _backslash_escape(source, index)
            continue
        if char == "\n":
            tokens.append(_Token("sep", "\n"))
            index += 1
            continue
        if char == "#":
            index = source.find("\n", index)
            if index < 0:
                break
            continue
        if source.startswith("<#", index):
            end = source.find("#>", index + 2)
            if end < 0:
                raise _Unparsed
            index = end + 2
            continue
        if char in {";"}:
            tokens.append(_Token("sep", ";"))
            index += 1
            continue
        if char == "|":
            tokens.append(_Token("pipe", "|"))
            index += 1
            continue
        if char == "{":
            tokens.append(_Token("lbrace", "{"))
            index += 1
            continue
        if char == "}":
            tokens.append(_Token("rbrace", "}"))
            index += 1
            continue
        if source.startswith("$(", index):
            tokens.append(_Token("sub", "$("))
            index += 2
            continue
        if source.startswith("@(", index):
            tokens.append(_Token("array", "@("))
            index += 2
            continue
        if char == "(":
            tokens.append(_Token("lparen", "("))
            index += 1
            continue
        if char == ")":
            tokens.append(_Token("rparen", ")"))
            index += 1
            continue
        if char in {"'", '"'}:
            index = _string(source, index, tokens)
            continue
        if char == "@" and index + 1 < length and source[index + 1] in {"'", '"'}:
            index = _here_string(source, index, tokens)
            continue
        created = re.match(r"\[scriptblock\]::create", source[index:], flags=re.IGNORECASE)
        if created is not None:
            tokens.append(_Token("dynamic", created.group(0)))
            index += created.end()
            continue
        redirect = _REDIR.match(source, index)
        if redirect is not None:
            kind = "merge" if redirect.group("merge") else "redirect"
            tokens.append(_Token(kind, redirect.group(0)))
            index = redirect.end()
            continue
        if char == "&":
            tokens.append(_Token("call", "&"))
            index += 1
            continue
        if char == "=":
            tokens.append(_Token("assign", "="))
            index += 1
            continue
        if char in {"%", "?"}:
            tokens.append(_Token("word", char))
            index += 1
            continue
        if char == "$":
            index = _variable(source, index, tokens)
            continue
        if char == "." and not _glued_dot(source, index):
            tokens.append(_Token("word", "."))
            index += 1
            continue
        word, index = _word(source, index)
        if word is not None:
            tokens.append(_Token("word", word))
            continue
        index += 1
    return tokens


def _backslash_escape(source: str, index: int) -> int:
    if index + 1 < len(source) and source[index + 1] == "\n":
        return index + 2
    return min(index + 2, len(source))


def _string(source: str, index: int, tokens: list[_Token]) -> int:
    quote = source[index]
    if quote == "'":
        end, literal = _single_quoted(source, index)
        tokens.append(_Token("string", literal))
        return end
    return _double_quoted(source, index, tokens)


def _single_quoted(source: str, index: int) -> tuple[int, str]:
    chars: list[str] = []
    index += 1
    while index < len(source):
        if source.startswith("''", index):
            chars.append("'")
            index += 2
            continue
        if source[index] == "'":
            return index + 1, "".join(chars)
        chars.append(source[index])
        index += 1
    raise _Unparsed


def _double_quoted(source: str, index: int, tokens: list[_Token]) -> int:
    index += 1
    literal: list[str] = []
    subs: list[_Token] = []
    while index < len(source):
        if source[index] == "`":
            if index + 1 < len(source):
                literal.append(source[index + 1])
            index += 2
            continue
        if source[index] == '"':
            if subs:
                tokens.extend(subs)
            else:
                tokens.append(_Token("string", "".join(literal)))
            return index + 1
        if source.startswith("$(", index):
            end = _matching_paren(source, index + 1)
            subs.append(_Token("sub", "$("))
            subs.extend(_tokenize(source[index + 2 : end - 1]))
            subs.append(_Token("rparen", ")"))
            index = end
            continue
        literal.append(source[index])
        index += 1
    raise _Unparsed


def _here_string(source: str, index: int, tokens: list[_Token]) -> int:
    quote = source[index + 1]
    if index + 2 >= len(source) or source[index + 2] != "\n":
        raise _Unparsed
    body = index + 3
    closer = f"\n{quote}@"
    end = source.find(closer, body)
    if end < 0:
        raise _Unparsed
    if quote == '"':
        tokens.extend(_double_quoted_body(source[body:end]))
    return end + len(closer)


def _double_quoted_body(body: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    while index < len(body):
        if body[index] == "`":
            index += 2
            continue
        if body.startswith("$(", index):
            end = _matching_paren(body, index + 1)
            tokens.append(_Token("sub", "$("))
            tokens.extend(_tokenize(body[index + 2 : end - 1]))
            tokens.append(_Token("rparen", ")"))
            index = end
            continue
        index += 1
    return tokens


def _matching_paren(source: str, index: int) -> int:
    """`index` 指着 `(`，返回匹配的 `)` 之后的位置。"""

    depth = 0
    while index < len(source):
        if source.startswith("<#", index):
            end = source.find("#>", index + 2)
            if end < 0:
                raise _Unparsed
            index = end + 2
            continue
        char = source[index]
        if char == "#":
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline
            continue
        if char == "'":
            index, _literal = _single_quoted(source, index)
            continue
        if char == '"':
            index = _double_quoted(source, index, [])
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise _Unparsed


def _variable(source: str, index: int, tokens: list[_Token]) -> int:
    if index + 1 < len(source) and source[index + 1] == "{":
        end = source.find("}", index + 2)
        if end < 0:
            raise _Unparsed
        index = end + 1
    else:
        index += 1
        while index < len(source) and _is_name(source[index]):
            index += 1
    tokens.append(_Token("var", ""))
    return _member_suffix(source, index, tokens)


def _member_suffix(source: str, index: int, tokens: list[_Token]) -> int:
    if source.startswith("::", index):
        start = index + 2
        end = start
        while end < len(source) and _is_name(source[end]):
            end += 1
        if end > start:
            tokens.append(_Token("member", source[start:end]))
            return end
    if index < len(source) and source[index] == "." and index + 1 < len(source) and _is_name(source[index + 1]):
        end = index + 1
        while end < len(source) and _is_name(source[end]):
            end += 1
        tokens.append(_Token("member", source[index + 1 : end]))
        return end
    return index


def _word(source: str, index: int) -> tuple[str | None, int]:
    start = index
    while index < len(source) and _is_word(source[index]):
        index += 1
    if index == start:
        return None, index
    return source[start:index], index


def _glued_dot(source: str, index: int) -> bool:
    return index + 1 < len(source) and source[index + 1] not in " \t\r\n;|{}()\"'`"


def _is_name(char: str) -> bool:
    return char.isalnum() or char in {"_", "-"}


def _is_word(char: str) -> bool:
    return char.isalnum() or char in {"_", "-", "\\", ":", ".", "/"}
