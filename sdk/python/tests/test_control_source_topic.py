import pytest

from r2kernel.core.control_source_topic import encode_source_topic


def test_source_topic_envelope_uses_utf8_lowercase_hex_and_preserves_topic():
    assert encode_source_topic("component.owner", "servo_joint") == (
        "__r2kernel_source_v1__:636f6d706f6e656e742e6f776e6572:servo_joint"
    )
    assert encode_source_topic("カメラ", "測距/scan") == (
        "__r2kernel_source_v1__:e382abe383a1e383a9:測距/scan"
    )


@pytest.mark.parametrize("source", ["", "a" * 129, "bad\0source", "\ud800"])
def test_source_name_requires_bounded_nul_free_utf8(source):
    with pytest.raises(ValueError):
        encode_source_topic(source, "control")


@pytest.mark.parametrize("topic", ["", "x" * 1025, "bad\0topic", "__r2kernel_source_v2__:x:y"])
def test_topic_requires_bounded_nul_free_nonreserved_text(topic):
    with pytest.raises(ValueError):
        encode_source_topic("component.owner", topic)


def test_source_topic_limits_are_measured_in_utf8_bytes():
    assert encode_source_topic("é" * 64, "topic")
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        encode_source_topic("é" * 65, "topic")
    assert encode_source_topic("source", "é" * 512)
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        encode_source_topic("source", "é" * 513)
