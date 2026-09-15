"""内核：agent 循环本体。

本模块不读环境变量、不创建网络客户端——只接收构造好的 llm / tools /
ask_permission。因此内核完全可测，无需任何环境准备。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from pydantic import ValidationError

from . import permission
from .events import Event, Failed, Finished, TextDelta, ToolCallStarted, ToolResult
from .llm import ToolCall
from .permission import AskPermission
from .tools import TOOLS, Tool, audit, tools_schema, truncate

DEFAULT_MAX_STEPS = 25


@dataclass
class ExecResult:
    """执行层内部产物。不是协议事件 ToolResult——执行层不该知道事件的存在。"""
    ok: bool
    content: str


class Agent:
    def __init__(
        self,
        llm,
        ask_permission: AskPermission,
        tools: dict[str, Tool] | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self.llm = llm
        self.ask_permission = ask_permission
        self.tools = TOOLS if tools is None else tools
        self.max_steps = max_steps
        self.messages: list[dict] = []

    async def execute(self, call: ToolCall) -> ExecResult:
        """执行一次工具调用。**永不抛异常**——失败一律转为结果回填。

        包一层 _run 是为了保证审计一定落盘：每条返回路径都要记日志，
        散在各个 return 前迟早漏一个。
        """
        result = await self._run(call)
        audit(call.name, call.args, result.ok, result.content)
        return result

    async def _run(self, call: ToolCall) -> ExecResult:
        tool = self.tools.get(call.name)
        if tool is None:
            # 模型会幻觉出不存在的工具名，这是常规路径而非异常
            return ExecResult(False, f"没有名为 {call.name} 的工具")

        if call.args_error:
            return ExecResult(False, f"参数错误：{call.args_error}")

        try:
            denied = await permission.check(tool, call.args, self.ask_permission)
        except Exception as exc:  # noqa: BLE001
            # 权限检查自身失败必须**按拒绝处理**（fail-closed）。远端 ask 的
            # 网络错误若直接抛出去，会毁掉整个 run，且默认放行是危险的。
            denied = f"权限检查失败，已按拒绝处理：{exc}"
        if denied:
            return ExecResult(False, denied)

        try:
            validated = tool.params(**call.args)
        except ValidationError as exc:
            return ExecResult(False, f"参数错误：{exc}")

        try:
            output = await tool.fn(**validated.model_dump())
        except Exception as exc:  # noqa: BLE001 — 工具的任何异常都只该是一次失败
            return ExecResult(False, f"工具报错：{exc}")

        return ExecResult(True, truncate(output))

    async def run(self, user_input: str) -> AsyncIterator[Event]:
        """驱动循环，产出事件流。这是内核唯一的对外入口。"""
        self.messages.append({"role": "user", "content": user_input})

        for _ in range(self.max_steps):
            reply = None
            try:
                async for item in self.llm.chat(self.messages, tools_schema()):
                    if isinstance(item, TextDelta):
                        yield item
                    else:
                        reply = item
            except Exception as exc:  # noqa: BLE001
                # spec §7 决定 3：API 超时/失败、鉴权失败属于**预期内失败**，
                # 必须变成 Failed 事件而不是抛穿生成器——否则 CLI 直接吃 traceback，
                # 客户端也拿不到任何结构化信号。网络中断、密钥错误、畸形 chunk 都走这里。
                yield Failed(f"模型调用失败：{exc}")
                return

            if reply is None:
                yield Failed("模型没有返回任何内容")
                return

            # 必须回填含 tool_calls 的 assistant 消息，
            # 否则模型下一轮会重复调用同一工具
            self.messages.append(reply.to_message())

            if not reply.tool_calls:
                yield Finished(reply.content)
                return

            for tool_call in reply.tool_calls:
                yield ToolCallStarted(
                    tool_call.id, tool_call.name, tool_call.args)
                result = await self.execute(tool_call)
                yield ToolResult(
                    tool_call.id, tool_call.name, result.ok, result.content)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result.content,
                })

        yield Failed(f"超过最大步数 {self.max_steps}")
