"""测试共用的断言。"""


def assert_no_retry_instruction(text: str) -> None:
    """这段话没有指示模型把 `dangerous` 设为 true 再调一次。"""

    assert "重新调用" not in text
    assert "设为 true" not in text
    assert "dangerous" not in text
