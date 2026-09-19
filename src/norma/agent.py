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

# 内核自带的 system prompt。
#
# 默认值放内核**而不是客户端**：放客户端的话，下一个客户端（v2 的浏览器客户端）
# 会再次漏掉它，而"中文提问先用英文答"（backlog 摩擦表 2026-09-16 有现场）就会原样复现。
# 每一句都在治一个**观察到的**毛病，没有凑数的客套话：
#   * 语种      —— 中文提问时它第一句用英文回答
#   * 工具选择  —— 它用 run_powershell 去"找文件"，而 list_dir 明明已经列出来了；
#                  exec 风险每次都会弹确认框，白弹一次
#   * 读取上限  —— 它试了 max_bytes 的 6万/20万/40万，因为没人告诉过它上限在哪
#   * 怎么翻页  —— 它为一个 16848 字符的文件连调 3 次 read_file 却只读到开头一半，
#                  因为每次都从第 0 个字符开始（v1.1.0 给了 offset，得让它知道）
#   * 不编造    —— 实测它已经做对了，把好行为写成契约
DEFAULT_SYSTEM_PROMPT = (
    "你是 norma，一个跑在用户自己电脑上的 AI 助手。\n"
    "始终用中文回答，除非用户明确要求别的语言。\n"
    "列目录、读文件、写文件要用对应的工具，不要用 run_powershell 去读文件"
    "（那是 exec，每次都会弹确认框，能绕开就绕开）。\n"
    "read_file 一次最多返回 8000 字符（开头的进度标记也算在内）；标记里的长度只是"
    "本次读到的，文件更大时会另有说明。没有可以调大这个上限的参数。\n"
    "长文件要分段读完：接着上次读到的地方继续，就把 offset 设成已读的字符数；"
    "不要重复读同一段。\n"
    "不确定就说不确定，绝不编造内容；读不到的部分要明说读不到。"
)


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
        system_prompt: str | None = None,
    ) -> None:
        self.llm = llm
        self.ask_permission = ask_permission
        self.tools = TOOLS if tools is None else tools
        self.max_steps = max_steps
        # None = 用内核默认；"" = 明确不要 system 消息。两种意图必须分得开。
        self.system_prompt = (
            DEFAULT_SYSTEM_PROMPT if system_prompt is None else system_prompt
        )
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

    def _ensure_system_message(self) -> None:
        """把 system 消息钉在 `messages[0]`，只钉一次。

        判据是**第一条是不是 system 消息**，而不是另记一个 `self._initialized` 布尔：
        这样客户端在构造后自己往 `messages` 里塞了东西（比如将来加载的历史）不会被覆盖，
        同时也不必维护第二份状态。

        为什么看位置而不只是"有没有"：`messages[0]` 是这条不变量本身。一个把历史
        恢复进 `messages`、system 却在中间的客户端，若按"存在即跳过"，我们会把
        默认 prompt 也跳掉——而中文提问先用英文答的缺陷正是靠这条默认值治的。
        （客户端自己带的 system 消息照样优先，包括带一条空的——`messages` 是唯一事实来源。）

        空 prompt 表示**真的不要** system 消息（`system_prompt=""`），那就一条都不插
        ——插一条空的 system 消息既没意义，也未必被服务端接受。
        """
        if not self.system_prompt:
            return
        if self.messages and self.messages[0].get("role") == "system":
            return
        self.messages.insert(0, {"role": "system", "content": self.system_prompt})

    async def run(self, user_input: str) -> AsyncIterator[Event]:
        """驱动循环，产出事件流。这是内核唯一的对外入口。"""
        self._ensure_system_message()
        self.messages.append({"role": "user", "content": user_input})

        for _ in range(self.max_steps):
            reply = None
            # schema 生成放在 try 之外：它是我们自己的代码，出错属代码 bug，应当抛出，
            # 而不是被下面的 except 伪装成一次"模型调用失败"——那会把真正的病因藏起来。
            schemas = tools_schema(self.tools)
            try:
                async for item in self.llm.chat(self.messages, schemas):
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
