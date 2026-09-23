"""核心：拦截。判为危险的动作只在经人裁决后执行（ADR-0004）。

裁决发生在 Claude Code 里：PreToolUse hook 把调用交给人（`ask`），人同意后调用才会抵达服务端。
hook 看不到人的答复，于是在交出去之前留下一张只对这一次调用有效的裁决凭据；
调用抵达时凭据还在，就说明它经过了人的裁决。没有凭据的危险动作一律拦截——
没有任何参数能让模型自己放行，包括把 `dangerous` 改成 `true` 重试。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from computer_use.action_log import Intercepted
from computer_use.danger import Verdict
from computer_use.desktop import DesktopPort
from computer_use.pace import budget_block_message

TICKET_LIFETIME = timedelta(minutes=10)
"""人可能要过一会儿才回应确认；超过这么久才抵达的调用，按未经裁决处理。"""


def refer_to_human(desktop: DesktopPort, tool: str, arguments: Mapping[str, Any]) -> None:
    """hook 把调用交给人裁决之前，为这一次调用留下裁决凭据。"""

    desktop.put_ticket(_call_key(tool, arguments), datetime.now(timezone.utc))


def require_ruling(
    desktop: DesktopPort, tool: str, arguments: Mapping[str, Any], verdict: Verdict
) -> None:
    """判为危险的调用须凭一张未过期的裁决凭据才能执行，凭据随之作废；否则抛 `Intercepted`。

    `arguments` 须与模型发出、hook 看到的调用参数逐字段一致，差一个字段都不算同一次调用。
    """

    if not verdict.dangerous:
        return
    issued_at = desktop.take_ticket(_call_key(tool, arguments))
    if issued_at is not None and datetime.now(timezone.utc) - issued_at <= TICKET_LIFETIME:
        return
    reasons = "；".join(verdict.reasons)
    if tool == "resume":
        raise Intercepted(f"{reasons}。请原样重新调用，由人在 Claude Code 中确认")
    if any("已达预算" in reason for reason in verdict.reasons) and arguments.get("dangerous") is not True:
        raise Intercepted(budget_block_message())
    if arguments.get("dangerous") is True:
        raise Intercepted(
            f"判为危险动作（{reasons}），但这次调用没有经过人的裁决。"
            "危险动作只能由人在 Claude Code 的确认提示里放行：PreToolUse hook 没有安装、出错，"
            f"或人的确认晚于 {TICKET_LIFETIME.seconds // 60} 分钟时无法执行，原样重试不会改变结果"
        )
    raise Intercepted(
        f"判为危险动作（{reasons}）。如确需执行，把 dangerous 设为 true 重新调用，"
        "由人在 Claude Code 中确认"
    )


def _call_key(tool: str, arguments: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"tool": tool, "arguments": dict(arguments)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
