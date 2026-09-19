import json

from types import SimpleNamespace as NS

import httpx2

from openai import AsyncOpenAI

from norma.config import Config
from norma.llm import LLM, Reply, ToolCall, assemble


def chunk(content=None, tool_calls=None):
    """伪造一个流式 chunk。tool_calls=None 表示这一片没有工具调用。"""
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tool_calls))])


def tc(index, id=None, name=None, arguments=None):
    """伪造一个 tool_call 分片。真实 API 里，同一 index 的首片带 id 和 name，
    后续片这两项为 None，只有 arguments 继续追加。"""
    return NS(index=index, id=id, function=NS(name=name, arguments=arguments))


def test_text_only_stream():
    reply = assemble([chunk("你"), chunk("好"), chunk("！")])
    assert reply.content == "你好！"
    assert reply.tool_calls == []


def test_empty_choices_chunk_is_ignored():
    # 收尾 chunk 常常带 usage 且 choices 为空
    reply = assemble([chunk("嗨"), NS(choices=[])])
    assert reply.content == "嗨"
    assert reply.tool_calls == []


def test_single_tool_call_arriving_in_one_chunk():
    reply = assemble([
        chunk(tool_calls=[tc(0, id="c1", name="list_dir", arguments='{"path": "."}')]),
    ])
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0] == ToolCall(id="c1", name="list_dir", args={"path": "."})


def test_tool_call_arguments_fragmented_across_chunks():
    """参数被拆成字符串碎片——这是真实 API 的常态。"""
    reply = assemble([
        chunk(tool_calls=[tc(0, id="c1", name="write_file", arguments='{"pa')]),
        chunk(tool_calls=[tc(0, arguments='th": "a.tx')]),
        chunk(tool_calls=[tc(0, arguments='t", "content": "哈"}')]),
    ])
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0].args == {"path": "a.txt", "content": "哈"}
    assert reply.tool_calls[0].args_error == ""


def test_later_chunks_have_no_id_or_name():
    """首片之后 id / name 为 None。写成赋值而非累加会把已取到的工具名清空。"""
    reply = assemble([
        chunk(tool_calls=[tc(0, id="c1", name="read_file", arguments='{"path"')]),
        chunk(tool_calls=[tc(0, id=None, name=None, arguments=': "x.txt"}')]),
    ])
    assert reply.tool_calls[0].name == "read_file"
    assert reply.tool_calls[0].id == "c1"
    assert reply.tool_calls[0].args == {"path": "x.txt"}


def test_two_tool_calls_interleaved_by_index():
    reply = assemble([
        chunk(tool_calls=[tc(0, id="c1", name="list_dir", arguments='{"path"')]),
        chunk(tool_calls=[tc(1, id="c2", name="read_file", arguments='{"pa')]),
        chunk(tool_calls=[tc(0, arguments=': "."}')]),
        chunk(tool_calls=[tc(1, arguments='th": "b.txt"}')]),
    ])
    assert [c.id for c in reply.tool_calls] == ["c1", "c2"]
    assert reply.tool_calls[0].args == {"path": "."}
    assert reply.tool_calls[1].args == {"path": "b.txt"}


def test_truncated_arguments_record_args_error():
    """max_tokens 截断会让 arguments 断在半截。"""
    reply = assemble([
        chunk(tool_calls=[tc(0, id="c1", name="write_file", arguments='{"path": "a.tx')]),
    ])
    assert reply.tool_calls[0].args_error != ""
    assert reply.tool_calls[0].args == {}


def test_empty_arguments_string_gives_empty_dict():
    reply = assemble([chunk(tool_calls=[tc(0, id="c1", name="noop", arguments="")])])
    assert reply.tool_calls[0].args == {}
    assert reply.tool_calls[0].args_error == ""


def test_to_message_serializes_arguments_as_json_string():
    reply = Reply(content="", tool_calls=[ToolCall(id="c1", name="t", args={"a": 1})])
    msg = reply.to_message()
    assert msg["role"] == "assistant"
    raw = msg["tool_calls"][0]["function"]["arguments"]
    assert isinstance(raw, str)
    assert json.loads(raw) == {"a": 1}


def test_a_paging_offset_survives_the_message_round_trip():
    """区间读取多了一个"要原样带回去的大整数"，把这条缝也钉住。

    `offset` 是 v1.1.0 才有的参数，值通常是几千到几万（如 15934）。它会走
    ToolCall → `to_message()` → JSON 字符串 → 下一轮请求 → 服务端 → 再回来。
    这中间任何一步把数字变成字符串、或者丢精度，模型下一轮就会从错的位置读
    ——而循环本身不会报错，只会悄悄地少读/重读。

    用**边界值**而不是"随便一个数"：JSON 里整数是安全的，但只有真跑一遍才知道
    我们的实现有没有把它当字符串拼。
    """
    for offset in (0, 1, 7969, 15934):
        reply = Reply(content="", tool_calls=[
            ToolCall(id="c1", name="read_file",
                     args={"path": "a.txt", "offset": offset, "limit": 8000}),
        ])
        raw = reply.to_message()["tool_calls"][0]["function"]["arguments"]

        assert isinstance(raw, str)               # 必须是 JSON 字符串，不是 dict
        back = json.loads(raw)
        assert back["offset"] == offset           # 原样回去，没变成 "15934"
        assert isinstance(back["offset"], int)    # 也没变成字符串
        assert back["limit"] == 8000

    # 顺带确认没被截断/转义弄坏：中文路径与负数（pydantic 会拦，但传输层不该自己改）
    reply = Reply(content="", tool_calls=[
        ToolCall(id="c1", name="read_file", args={"path": "小说/第一章.txt", "offset": 0}),
    ])
    raw = reply.to_message()["tool_calls"][0]["function"]["arguments"]
    assert json.loads(raw)["path"] == "小说/第一章.txt"
    assert "\\u" not in raw                       # ensure_ascii=False：不该被转义成 \uXXXX


def test_to_message_keeps_empty_content_as_none():
    reply = Reply(content="", tool_calls=[ToolCall(id="c1", name="t", args={})])
    assert reply.to_message()["content"] is None


def test_reply_without_tool_calls_omits_the_key():
    """空 tool_calls 数组在部分服务端会被判为非法，干脆不带这个键。"""
    msg = Reply(content="你好").to_message()
    assert msg == {"role": "assistant", "content": "你好"}


# ---------- LLM.chat 的请求体（离线，无网络） ----------

def _llm_capturing(body_out: dict) -> LLM:
    """构造一个把请求体写进 body_out 的 LLM。

    用 httpx2.MockTransport 在传输层拦截——不发真实请求，也不需要网络。
    替换内部客户端是刻意的：为了不动 LLM 的公开接口（v1 不需要客户端注入缝）。
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        body_out["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "m",
                "choices": [
                    {"index": 0, "delta": {"content": "hi"}, "finish_reason": None}
                ],
            },
            headers={"content-type": "application/json"},
        )

    config = Config(base_url="https://example.invalid/v1", api_key="sk-x", model="m")
    llm = LLM(config)
    llm._client = AsyncOpenAI(
        base_url=config.base_url,
        api_key=config.api_key,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return llm


def _tools() -> list[dict]:
    return [{"type": "function", "function": {"name": "t", "description": "d",
                                              "parameters": {"type": "object"}}}]


async def test_chat_omits_tools_key_when_no_tools():
    """没有工具时请求体里不该有 tools 键——传 None 会变成 "tools": null。"""
    captured: dict = {}
    llm = _llm_capturing(captured)
    async for _ in llm.chat([{"role": "user", "content": "x"}], []):
        pass
    assert "tools" not in captured["body"]


async def test_chat_sends_tools_key_when_tools_present():
    captured: dict = {}
    llm = _llm_capturing(captured)
    async for _ in llm.chat([{"role": "user", "content": "x"}], _tools()):
        pass
    assert captured["body"]["tools"] == _tools()


def test_client_has_a_finite_request_timeout():
    """SDK 默认 600 秒 + 重试，网络卡住时 CLI 会静默假死半小时。

    这里断言的是我们**确实设了**一个远小于默认值的超时。若 SDK 内部把
    数值规范化成别的形态，就断言它规范化后的实际值，不要删掉这条断言。
    """
    from norma.llm import REQUEST_TIMEOUT_S

    llm = LLM(Config(base_url="https://example.invalid/v1", api_key="sk-x", model="m"))
    assert REQUEST_TIMEOUT_S <= 60.0
    assert llm._client.timeout == REQUEST_TIMEOUT_S
