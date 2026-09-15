"""openai SDK 适配器。

本模块是内核里唯一碰网络的地方，也是测试的注入接缝。
除了 ``assemble`` 之外没有真实逻辑——它把流式 chunk 拼成 Reply。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import AsyncIterator

from openai import AsyncOpenAI

from .config import Config
from .events import TextDelta

# openai SDK 默认 600 秒超时 + 2 次重试：网络卡住时 CLI 会静默假死半小时，
# 对每天用的终端助手来说与死机无异。
REQUEST_TIMEOUT_S = 60.0


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    args_error: str = ""  # 非空 = arguments 不是合法 JSON


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def to_message(self) -> dict:
        """转成 OpenAI 的 assistant 消息。

        arguments 必须是 **JSON 字符串**而不是 dict。塞 dict 是常见的静默
        错误：部分服务端宽容接受，直到换模型或参数含特殊字符时才报错。

        没有工具调用时不带 tool_calls 键——空数组在部分服务端会被判为非法。
        """
        message = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": json.dumps(c.args, ensure_ascii=False),
                    },
                }
                for c in self.tool_calls
            ]
        return message


@dataclass
class _Partial:
    """一个正在拼接中的 tool_call。"""
    id: str = ""
    name: str = ""
    args_json: str = ""


def assemble(chunks: list) -> Reply:
    """把流式 chunk 序列拼成一个 Reply。

    真实 API 会把 tool_call 的 arguments 拆成字符串碎片，按 index 分片下发；
    同一 index 的首片带 ``id`` 和 ``function.name``，后续片这两项为 None，
    只有 ``function.arguments`` 继续追加。因此这里必须：
      * 按 index 分别累积
      * 对 id / name 用条件累加，不能直接赋值（否则后续片的 None 会覆盖）
      * 全部收齐之后才 json.loads
    """
    content_parts: list[str] = []
    partials: dict[int, _Partial] = {}

    for chunk in chunks:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        delta = choices[0].delta
        if delta.content:
            content_parts.append(delta.content)
        for frag in (delta.tool_calls or []):
            partial = partials.setdefault(frag.index, _Partial())
            if frag.id:
                partial.id = frag.id
            func = frag.function
            if func is not None:
                if func.name:
                    partial.name += func.name
                if func.arguments:
                    partial.args_json += func.arguments

    calls: list[ToolCall] = []
    for index in sorted(partials):
        partial = partials[index]
        args: dict = {}
        args_error = ""
        if partial.args_json:
            try:
                parsed = json.loads(partial.args_json)
            except json.JSONDecodeError as exc:
                args_error = f"arguments 不是合法 JSON（{exc}）"
            else:
                if isinstance(parsed, dict):
                    args = parsed
                else:
                    args_error = f"arguments 不是 JSON 对象，而是 {type(parsed).__name__}"
        calls.append(ToolCall(
            id=partial.id, name=partial.name, args=args, args_error=args_error))

    return Reply(content="".join(content_parts), tool_calls=calls)


class LLM:
    def __init__(self, config: Config) -> None:
        self._client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=REQUEST_TIMEOUT_S,
        )
        self._model = config.model

    async def chat(
        self, messages: list[dict], tools: list[dict]
    ) -> AsyncIterator[TextDelta | Reply]:
        """先流式吐若干 TextDelta，最后吐一个 Reply。"""
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            stream=True,
            # 没有工具时**不带这个键**——不是传 None。实测 openai 3.14.0 会把
            # tools=None 原样序列化成 "tools": null 上线，而严格校验的兼容网关
            # 会因此返回 400。条件展开让"没有工具"真的等于"没有这个键"。
            **({"tools": tools} if tools else {}),
        )
        collected = []
        async for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if choices and choices[0].delta.content:
                yield TextDelta(choices[0].delta.content)
            collected.append(chunk)
        yield assemble(collected)
