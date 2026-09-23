"""核心：动作日志与留证。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping, TypeVar

from computer_use.desktop import DesktopPort
from computer_use.windows import operable_windows

T = TypeVar("T")


class Intercepted(Exception):
    """动作在抵达桌面之前被拦下；消息是拦截理由，原样回给模型。"""


class ActionLog:
    """每次工具调用追加一条记录。

    被拦截或失败的调用额外留一张目标窗口当时的截图；成功的不留，
    免得聊天内容与密码堆在磁盘上。
    """

    def __init__(self, desktop: DesktopPort) -> None:
        self._desktop = desktop

    def run(
        self,
        *,
        tool: str,
        target: Mapping[str, Any],
        intent: str | None,
        evidence_window: int | None,
        action: Callable[[], T],
    ) -> T:
        """执行 `action` 并记下结果；`action` 抛出的异常记录后原样抛出。

        `target` 原样写进日志；`evidence_window` 是留证时要截的窗口，没有时为 `None`。
        """

        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "target": dict(target),
            "intent": intent,
        }
        try:
            result = action()
        except Intercepted as reason:
            self._append(
                record,
                verdict="intercepted",
                outcome="not_executed",
                detail=str(reason),
                evidence=self._evidence(evidence_window),
            )
            raise
        except Exception as error:
            self._append(
                record,
                verdict="allowed",
                outcome="failed",
                detail=str(error),
                evidence=self._evidence(evidence_window),
            )
            raise
        self._append(
            record, verdict="allowed", outcome="succeeded", detail=None, evidence=None
        )
        return result

    def _evidence(self, window: int | None) -> str | None:
        """截下目标窗口并存盘，返回位置；截不到或存不下时为 `None`。

        只截可操作的窗口：工具不肯给模型看的窗口，也不该因为一次失败的调用落到磁盘上。
        留证是尽力而为，它自己出错不能盖住调用本身的错误，也不能让这条记录丢掉。
        """

        if window is None or window not in {w.handle for w in operable_windows(self._desktop)}:
            return None
        try:
            png = io.BytesIO()
            self._desktop.capture_window(window).image.save(png, format="PNG")
            return self._desktop.save_evidence(png.getvalue())
        except Exception:
            return None

    def _append(
        self,
        record: dict[str, Any],
        *,
        verdict: Literal["allowed", "intercepted"],
        outcome: Literal["succeeded", "failed", "not_executed"],
        detail: str | None,
        evidence: str | None,
    ) -> None:
        self._desktop.append_log(
            json.dumps(
                {
                    **record,
                    "verdict": verdict,
                    "outcome": outcome,
                    "detail": detail,
                    "evidence": evidence,
                },
                ensure_ascii=False,
            )
        )
