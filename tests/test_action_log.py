"""动作日志与留证：每次调用一条记录，被拦截或失败的调用留下当时的画面。"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
from PIL import Image

from computer_use.action_log import ActionLog, Intercepted
from computer_use.desktop import Rect, Window

from .fake_desktop import FakeDesktop, window


def test_成功的调用追加一条结构化日志_不留截图() -> None:
    desktop = FakeDesktop([window(handle=1)])
    log = ActionLog(desktop)

    before = datetime.now(timezone.utc)
    result = log.run(
        tool="click",
        target={"window": 1, "x": 10, "y": 20},
        intent="点记事本的编辑区",
        dangerous=False,
        evidence_window=1,
        action=lambda: "点到了",
    )
    after = datetime.now(timezone.utc)

    assert result == "点到了"
    [record] = desktop.action_log()
    assert before <= datetime.fromisoformat(record.pop("time")) <= after
    assert record == {
        "tool": "click",
        "target": {"window": 1, "x": 10, "y": 20},
        "intent": "点记事本的编辑区",
        "dangerous": False,
        "verdict": "allowed",
        "outcome": "succeeded",
        "detail": None,
        "evidence": None,
    }
    assert desktop.evidence == {}


def test_失败的调用记下错误并留下目标窗口当时的截图() -> None:
    frame = Image.new("RGB", (320, 240), "red")
    desktop = FakeDesktop(
        [window(handle=1, rect=Rect(0, 0, 320, 240))], images={1: frame}
    )
    log = ActionLog(desktop)

    def fail() -> None:
        raise RuntimeError("SendInput 失败")

    with pytest.raises(RuntimeError, match="SendInput 失败"):
        log.run(tool="click", target={"window": 1}, intent="点一下", dangerous=None, evidence_window=1, action=fail)

    [record] = desktop.action_log()
    assert record["verdict"] == "allowed"
    assert record["outcome"] == "failed"
    assert "SendInput 失败" in record["detail"]
    evidence = _decode(desktop.evidence[record["evidence"]])
    assert evidence.size == (320, 240)
    assert evidence.getcolors() == [(320 * 240, (255, 0, 0))]


def test_被拦截的调用记下拦截理由并留下目标窗口当时的截图() -> None:
    desktop = FakeDesktop(
        [window(handle=1, rect=Rect(0, 0, 320, 240))],
        images={1: Image.new("RGB", (320, 240), "red")},
    )
    log = ActionLog(desktop)

    def intercept() -> None:
        raise Intercepted("落点在任务作用域之外")

    with pytest.raises(Intercepted):
        log.run(tool="click", target={"window": 1}, intent="点发送", dangerous=None, evidence_window=1, action=intercept)

    [record] = desktop.action_log()
    assert record["intent"] == "点发送"
    assert record["verdict"] == "intercepted"
    assert record["outcome"] == "not_executed"
    assert record["detail"] == "落点在任务作用域之外"
    assert _decode(desktop.evidence[record["evidence"]]).size == (320, 240)


def test_目标窗口已无法截图时照常记录_原错误照常抛出() -> None:
    desktop = FakeDesktop([window(handle=1)], gone={1})
    log = ActionLog(desktop)

    def fail() -> None:
        raise RuntimeError("窗口已关闭")

    with pytest.raises(RuntimeError, match="窗口已关闭"):
        log.run(tool="click", target={"window": 1}, intent=None, dangerous=None, evidence_window=1, action=fail)

    [record] = desktop.action_log()
    assert (record["outcome"], record["evidence"]) == ("failed", None)
    assert desktop.evidence == {}


@pytest.mark.parametrize(
    "hidden",
    [
        window(handle=1, is_minimized=True),
        window(handle=1, is_visible=False),
        window(handle=1, title=""),
    ],
    ids=["最小化的窗口", "不可见的窗口", "无标题的窗口"],
)
def test_不为不可操作的窗口留证(hidden: Window) -> None:
    desktop = FakeDesktop([hidden])
    log = ActionLog(desktop)

    def fail() -> None:
        raise RuntimeError("窗口不可操作")

    with pytest.raises(RuntimeError):
        log.run(tool="observe_window", target={"window": 1}, intent=None, dangerous=None, evidence_window=1, action=fail)

    [record] = desktop.action_log()
    assert (record["outcome"], record["evidence"]) == ("failed", None)
    assert desktop.evidence == {}


def test_留证截图存不下时照常记录_原错误照常抛出() -> None:
    class DiskFull(FakeDesktop):
        def save_evidence(self, png: bytes) -> str:
            raise OSError("磁盘已满")

    desktop = DiskFull([window(handle=1)])
    log = ActionLog(desktop)

    def fail() -> None:
        raise RuntimeError("SendInput 失败")

    with pytest.raises(RuntimeError, match="SendInput 失败"):
        log.run(tool="click", target={"window": 1}, intent=None, dangerous=None, evidence_window=1, action=fail)

    [record] = desktop.action_log()
    assert (record["outcome"], record["evidence"]) == ("failed", None)


def _decode(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")
