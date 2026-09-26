"""核心：外发动作、切换任务作用域、永久删除，以及未自报高危词的原生确认。

常规危险动作走 PreToolUse hook。外发动作、切换任务作用域、永久删除，
以及模型自报不危险但落点附近读到高危词，都在执行前弹一个系统对话框。
模型正在等这次调用返回，插不进手。拒绝和超时都不执行。
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL.Image import Image

from computer_use.action_log import Intercepted
from computer_use.danger import nearby_risk_outside_outbound, outbound_hits, sends_content
from computer_use.desktop import DesktopPort, Window, WindowUnavailable
from computer_use.scope import TaskScope

CONFIRM_TIMEOUT = 60.0
"""人要看截图和已输入的内容，给一分钟。过了按拒绝。"""


@dataclass(frozen=True)
class ConfirmSubject:
    """一次原生确认要摆给人的落点：意图、附近读到的字、窗口。"""

    intent: str
    nearby: str | None
    window: Window


def confirm_nearby_risk_outside_outbound(desktop: DesktopPort, spot: ConfirmSubject) -> None:
    """模型自报不危险、落点附近有高危词、且外发确认不会问这些词时，同一次调用里弹原生对话框。

    外发词由 `confirm_outbound` 问。没有这类词时不弹。人拒绝或超时抛 `Intercepted`，动作不得执行。
    """

    words = nearby_risk_outside_outbound(spot.nearby)
    if not words:
        return
    listed = "、".join(words)
    _ask(
        desktop,
        title="高危词需要确认",
        message=(
            f"落点附近读到高危词（{listed}）。\n"
            f"意图：{spot.intent}\n"
            f"窗口：{_window_label(spot.window)}\n"
            "拒绝或超时则不执行。"
        ),
        image=None,
        rejected="用户拒绝了这次高危词确认，动作未执行",
        timed_out="确认对话框超时，按拒绝处理，动作未执行",
    )


def confirm_outbound(desktop: DesktopPort, scope: TaskScope, spot: ConfirmSubject) -> None:
    """落点附近或意图指向外发时，执行前弹原生对话框。人拒绝或超时抛 `Intercepted`。

    发送类同时摆上当前窗口截图和已经打进这个窗口的文本。
    """

    hits = outbound_hits(spot.intent, spot.nearby)
    if not hits:
        return
    image = None
    entered = ""
    if sends_content(hits):
        entered = scope.entered_text(spot.window.handle)
        image = _current_image(desktop, spot.window)
    words = "、".join(hits)
    message = (
        f"即将执行外发动作（{words}）。\n"
        f"意图：{spot.intent}\n"
        f"窗口：{_window_label(spot.window)}"
    )
    if sends_content(hits):
        message += f"\n已输入的内容：\n{entered or '（无）'}"
    _ask(
        desktop,
        title="外发动作需要确认",
        message=message,
        image=image,
        rejected="用户拒绝了这次外发动作，动作未执行",
        timed_out="确认对话框超时，按拒绝处理，外发动作未执行",
    )
    if sends_content(hits):
        scope.forget_text(spot.window.handle)


def confirm_permanent_delete(desktop: DesktopPort, path: str) -> None:
    """永久删除在常规拦截通过之后再弹原生对话框。人拒绝或超时抛 `Intercepted`，文件不得被删。"""

    _ask(
        desktop,
        title="永久删除需要确认",
        message=(
            "即将永久删除，文件不会进入回收站。\n"
            f"目标路径：{path}\n"
            "拒绝或超时则文件保持原样。"
        ),
        image=None,
        rejected="用户拒绝了这次永久删除，文件保持原样",
        timed_out="确认对话框超时，按拒绝处理，文件保持原样",
    )


def confirm_scope_switch(
    desktop: DesktopPort, before: tuple[Window, ...], after: tuple[Window, ...]
) -> None:
    """已经有任务作用域、又要换成另一组窗口时，替换前弹原生对话框。

    第一次声明不是切换。人拒绝或超时抛 `Intercepted`，调用方须保持原作用域。
    """

    if not before or {w.handle for w in before} == {w.handle for w in after}:
        return
    message = (
        "要把任务作用域从\n"
        f"{_list(before)}\n"
        "换成\n"
        f"{_list(after)}\n"
        "吗？拒绝或超时则保持原作用域。"
    )
    _ask(
        desktop,
        title="切换任务作用域需要确认",
        message=message,
        image=None,
        rejected="用户拒绝切换任务作用域，作用域未改变",
        timed_out="确认对话框超时，按拒绝处理，任务作用域未改变",
    )


def _ask(
    desktop: DesktopPort,
    *,
    title: str,
    message: str,
    image: Image | None,
    rejected: str,
    timed_out: str,
) -> None:
    reply = desktop.confirm(title=title, message=message, image=image, timeout=CONFIRM_TIMEOUT)
    if reply is True:
        return
    raise Intercepted(timed_out if reply is None else rejected)


def _current_image(desktop: DesktopPort, window: Window) -> Image | None:
    try:
        return desktop.capture_window(window.handle).image
    except WindowUnavailable:
        return None


def _window_label(window: Window) -> str:
    return f"「{window.title}」（{window.process_name or '未知进程'}，句柄 {window.handle}）"


def _list(windows: tuple[Window, ...]) -> str:
    return "\n".join(f"窗口{_window_label(window)}" for window in windows)
