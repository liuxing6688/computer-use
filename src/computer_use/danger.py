"""核心：危险动作判定（ADR-0003）。两条判据取并集，任一为真即判为危险。

- 模型自报：`judge_call` 只看调用参数，hook 与服务端共用它。
- 落点附近的文字：`judge_nearby_text` 看 OCR 读出的字，只有握着截图的服务端能用。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from computer_use.desktop import DesktopPort, Rect, TextUnreadable
from computer_use.observation import Screenshot

FILE_MUTATIONS = frozenset({"write_file", "move_file", "delete_file"})
"""一律须经人确认的文件变更。模型把 dangerous 报成 false 也不能自己放行。"""

_FILE_REASONS = {
    "write_file": "写入文件须经人确认",
    "move_file": "移动文件须经人确认",
    "delete_file": "删除文件须经人确认",
}

INPUT_TOOLS = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "drag",
        "scroll",
        "type_text",
        "press_keys",
        "launch_app",
    }
)
"""须自报危险性的输入工具。"""

_CJK_WORDS = (
    "删除",
    "卸载",
    "移除",
    "清空",
    "格式化",
    "覆盖",
    "发送",
    "提交",
    "发布",
    "确定",
    "确认",
    "支付",
    "付款",
    "转账",
    "购买",
    "下单",
    "注销",
)
_LATIN_WORDS = (  # 不收 format：它是英文菜单栏的常客（记事本的 Format），收了每次点编辑区都要确认。
    "delete",
    "remove",
    "uninstall",
    "erase",
    "overwrite",
    "send",
    "submit",
    "publish",
    "ok",
    "confirm",
    "pay",
    "transfer",
    "purchase",
    "buy",
)

NEARBY = (200, 48)
"""落点周边做 OCR 的区域大小，逻辑像素（宽, 高），以落点为中心。约是一个按钮加上它两侧的余量。"""


@dataclass(frozen=True)
class Verdict:
    """一次判定的结果：判为危险的理由，没有理由即不危险。"""

    reasons: tuple[str, ...] = ()

    @property
    def dangerous(self) -> bool:
        return bool(self.reasons)

    def __or__(self, other: Verdict) -> Verdict:
        return Verdict(self.reasons + other.reasons)


def judge_call(tool: str, arguments: Mapping[str, Any]) -> Verdict:
    """凭调用参数判定。

    输入工具除非明确自报 `dangerous` 为 `false`，否则判为危险。
    写入、移动、删除一律判为危险，自报成 `false` 也不放行；永久删除另有一条更重的理由。
    """

    if tool in FILE_MUTATIONS:
        if tool == "delete_file" and arguments.get("permanent") is True:
            return Verdict(("永久删除须经人确认",))
        return Verdict((_FILE_REASONS[tool],))
    if tool == "run_powershell":
        from computer_use.powershell import judge_command

        command = arguments.get("command")
        if not isinstance(command, str):
            return Verdict(("无法解析命令，无从确认只读",))
        return judge_command(command)
    if tool not in INPUT_TOOLS:
        return Verdict()
    declared = arguments.get("dangerous")
    if declared is False:
        return Verdict()
    if declared is True:
        return Verdict(("模型自报为危险动作",))
    return Verdict(("模型没有明确自报危险性（dangerous 须为 true 或 false）",))


_OUTBOUND_CJK = ("发送", "提交", "发布", "支付", "付款", "转账", "购买", "下单")
_OUTBOUND_LATIN = ("send", "submit", "publish", "pay", "transfer", "purchase", "buy")
_SEND_WORDS = frozenset({"发送", "提交", "发布", "send", "submit", "publish"})
"""外发里「把已经输入的内容发出去」的那一类：确认时要同时摆上截图和这段内容。"""


def nearby_risk_words(text: str | None) -> tuple[str, ...]:
    """落点附近文字里的高危词，按出现顺序。`None` 表示没能读出来，没有词。

    OCR 常在汉字之间插空格，中文词去掉空白后按子串匹配；英文词按整词匹配，免得 `Book` 里读出 `ok`。
    """

    if text is None:
        return ()
    return _matched(text, _CJK_WORDS, _LATIN_WORDS)


def judge_nearby_text(text: str | None) -> Verdict:
    """凭落点附近读出的文字判定：含高危词即判为危险。`None` 表示没能读出来，不因此判为危险。"""

    return Verdict(tuple(f"落点附近有高危词「{w}」" for w in nearby_risk_words(text)))


def outbound_hits(*texts: str | None) -> tuple[str, ...]:
    """这些文字里指向外发动作的词，按出现顺序、去重。空串与 `None` 不命中。"""

    found: list[str] = []
    for text in texts:
        if text is None:
            continue
        for word in _matched(text, _OUTBOUND_CJK, _OUTBOUND_LATIN):
            if word not in found:
                found.append(word)
    return tuple(found)


def sends_content(hits: tuple[str, ...]) -> bool:
    """这些外发词里有没有「发出已输入内容」的那一类。"""

    return any(word in _SEND_WORDS for word in hits)


def _matched(text: str, cjk: tuple[str, ...], latin: tuple[str, ...]) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", text)
    lowered = text.lower()
    return tuple(w for w in cjk if w in compact) + tuple(
        w for w in latin if re.search(rf"\b{w}\b", lowered)
    )


def read_nearby(desktop: DesktopPort, screenshot: Screenshot, x: int, y: int) -> str | None:
    """截图 `screenshot` 背后的原始采集中，屏幕物理像素 `(x, y)` 周边写着的字；读不出时为 `None`。

    读的是模型决策时看的那一刻，而不是重新截图。
    """

    capture = screenshot.capture
    half_width = round(NEARBY[0] * capture.dpi_scale / 2)
    half_height = round(NEARBY[1] * capture.dpi_scale / 2)
    bounds = capture.rect
    left = max(bounds.left, x - half_width)
    top = max(bounds.top, y - half_height)
    right = min(bounds.left + bounds.width, x + half_width)
    bottom = min(bounds.top + bounds.height, y + half_height)
    try:
        return desktop.recognize_text(
            capture, Rect(left=left, top=top, width=right - left, height=bottom - top)
        )
    except TextUnreadable:
        return None
