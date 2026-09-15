import json

import pytest

from norma.events import (
    Event, Failed, Finished, TextDelta, ToolCallStarted, ToolResult,
)

ALL_EVENTS = [
    TextDelta("你好"),
    ToolCallStarted("c1", "list_dir", {"path": "."}),
    ToolResult("c1", "list_dir", True, "a.txt"),
    Finished("做完了"),
    Failed("API 超时"),
]


def test_every_event_subclasses_event():
    for event in ALL_EVENTS:
        assert isinstance(event, Event)


def test_to_dict_carries_type_name_and_fields():
    assert TextDelta("hi").to_dict() == {"type": "TextDelta", "text": "hi"}


def test_tool_result_to_dict_shape():
    assert ToolResult("c1", "list_dir", True, "out").to_dict() == {
        "type": "ToolResult",
        "call_id": "c1",
        "name": "list_dir",
        "ok": True,
        "content": "out",
    }


@pytest.mark.parametrize("event", ALL_EVENTS, ids=lambda e: type(e).__name__)
def test_every_event_is_json_serializable(event):
    # 防的是「某天有人往事件里塞了个 Path 对象」——这类错直到接手机端才炸
    json.dumps(event.to_dict(), ensure_ascii=False)
