# norma Agent 内核设计

日期：2026-09-15
状态：已确认，待实现
范围：v1 = 内核先行（里程碑一）

## 1. 背景与目标

norma 是一个**常驻的个人 AI 助手基站**，目标是支撑日常办公，让助手能远程在这台电脑上执行任务，并提供语音输入输出。

`norma` 仓库只负责**内核**：把「用户输入 → 模型推理 → 工具调用 → 拦截校验 → 执行 → 结果回填 → 继续推理 → 最终答案」这条循环做对、做稳、可扩展。

上层客户端（Electron 桌面端、Android、鸿蒙 ArkTS）在**其他仓库**实现，通过协议连接 norma。本仓库不含任何 UI。

### 为什么内核要做成服务

Android 与鸿蒙上无法运行 Python，所以 norma 只能是一个跑在用户电脑上的常驻服务，各端是薄客户端。这决定了一条核心设计原则：

> **norma 对外的公共接口是「协议」，不是 Python API。**

三个语言各异的客户端消费同一份事件协议。协议一旦将就，三个端都得跟着将就。因此事件流的形状是本设计中最先钉死的部分。

### 客户端拓扑：三个端都是 norma 的对等客户端

一个必须先钉死的问题：**手机端对接的是 norma 内核，不是 Electron 桌面端。**

```
Electron ─┐
Android  ─┼─→ norma（常驻服务，持有工具与机器权限）→ 在电脑上执行任务
HarmonyOS ┘
```

Electron 没有特殊地位，它只是三个前端之一。

**为什么不能让手机绕道桌面端：**

1. **工具在 norma 手里，不在 Electron 手里。** 任务要在电脑上执行，能执行的只有 norma 进程本身。
2. **绕道意味着「必须开着桌面窗口」手机才能用。** GUI 进程成为单点依赖——关掉窗口、崩溃、未启动，远程能力全部消失。
3. **三个端消费同一份协议，正是把内核做成事件流的目的。** 若手机走 Electron，Electron 就得把协议再代理一遍——第二个语言里的第二份协议实现，外加双份连接管理。

**Electron 的真实职责是进程生命周期，不是数据通路**：拉起 norma（sidecar）、托盘图标、开机自启、崩溃重启。这是「**谁把 norma 启动起来**」，与「**谁转发 norma 的流量**」是两件事，不可混淆。

**由此引出 v1 的一条硬约束：**

> 会话状态（`Agent.messages`）属于内核，**不得存在于 `cli.py`**。

v1 中 CLI 进程恰好等于内核进程，很容易顺手把状态写进 CLI。但远端接入时，内核必须能脱离任何客户端独立存活——**客户端是可插拔的，会话不是**。当前模块划分已满足此约束（`cli.py` 只构造 `Agent` 并驱动它），实现时守住这条线即可。

**刻意推迟**：手机如何触达电脑（局域网 / Tailscale 之类组网 / 公网中继）。这是网络接入问题，不改变内核形状，留到做传输层时再定。

### v1 的成功标准

对着终端说「帮我整理下载文件夹」，它真的去做了——能列出目录、读文件、写文件、执行命令，并在多轮里自己推进。

## 2. 范围

### v1 做

- async generator 事件流内核
- 工具注册表 + 4 个本机工具
- 权限闸门（缝留好，策略最简）
- CLI 客户端（交互式 REPL）
- 审计日志
- 自动化测试（见 §13）+ 1 次真机冒烟

### v1 不做（刻意推迟）

| 不做 | 什么时候做 |
|---|---|
| 网络传输层（WebSocket/HTTP）、鉴权 | 接入第一个远程客户端时 |
| 语音输入输出（ASR/TTS） | 内核稳定后 |
| 会话持久化（历史只存内存） | 需要跨进程恢复会话时 |
| token 用量统计 | 需要核算成本时 |
| 多 provider 抽象层 | 接入协议不同的第二家（如 Anthropic）时 |
| 规则文件 / 远端确认式权限策略 | 远程接入时（届时只改 `permission.py`） |
| 工具调用并发执行 | 串行成为可测量瓶颈时 |
| 上下文压缩 | 长会话触到窗口上限时 |

## 3. 技术选型与理由

**语言：Python 3.11+**

理由基于本项目的具体形态，而非泛泛的「生态好」：

1. **差异化能力是「在电脑上执行任务」**，这恰是 Python 生态最深、其他语言最浅的位置：`pywin32`（COM/WMI）、`uiautomation`、`pyautogui`、`openpyxl`、`python-docx`。纯 shell 操作别的语言也能做，但办公自动化很快越出这个范围，底座的能力上限会被语言生态直接封顶。
2. **语音的本地路径是 Python 的独角戏**：`faster-whisper` + `silero-vad` + `piper`/`edge-tts` 是既成路径。日常助手不应每句话都往返云端。
3. **工具注册表有更干净的答案**：pydantic 模型 → JSON Schema → 模型可用的 tool 定义，这条链类型安全、零手写，同时白送参数校验。

代价：前端客户端需另写，协议要手写一遍。但那是**一个薄客户端**，比用其他语言重写全部 OS 自动化与音频胶水便宜得多。

**模型接入**：`openai` SDK 指向可配置的 `base_url`。v1 以 DeepSeek 为主，**不做 provider 抽象层**——没有第二个实现就不要接口。换地址/换 key/换模型名是改配置，不是改代码。

## 4. 依赖

```
openai           # 传输层：流式响应、tool_call 增量解析
pydantic         # 工具参数校验 + schema 生成（openai 的传递依赖，不新增成本）
python-dotenv    # 读 .env，日常改配置不必 setx
--- dev ---
pytest
pytest-asyncio
```

**不新增其他依赖。** 不引入 agent 框架、不引入 Schema 库、不引入 CLI 框架（`argparse` 足够）。

关于 `python-dotenv` 的说明：stdlib 没有 `.env` 读取器，自己搓是重新发明。这是一个每天要用的工具，配置别扭就不会被用起来——这一条依赖是刻意的。

## 5. 文件结构

采用 `src/` 布局。扁平布局（`norma/norma/`）下从仓库根目录运行 `python` 时，`import norma` 拿到的是「当前目录碰巧同名的那份源码」而非已安装的包；两者不一致时会调试一个根本没在跑的版本。这个坑在打包给 Electron 做 sidecar 时会咬人。

```
norma/
├── pyproject.toml
├── .env.example
├── README.md
├── docs/superpowers/specs/2026-09-15-norma-agent-kernel-design.md
├── src/
│   └── norma/
│       ├── __init__.py
│       ├── config.py
│       ├── events.py
│       ├── llm.py
│       ├── tools.py
│       ├── permission.py
│       ├── agent.py
│       └── cli.py
└── tests/
    ├── test_events.py
    ├── test_config.py
    ├── test_llm.py
    ├── test_tools.py
    ├── test_permission.py
    └── test_agent.py
```

## 6. 模块职责与依赖方向

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `config.py` | 读环境变量：`base_url` / `api_key` / `model` / `max_steps` | 只做配置，不含逻辑 |
| `events.py` | **协议**：内核往外播报的 5 种事件 | 字段只用 JSON 原生类型 |
| `llm.py` | openai SDK 适配器 | 不认识工具实现、不认识权限 |
| `tools.py` | 工具定义 + 注册表 + 4 个工具实现 | 不认识循环 |
| `permission.py` | 权限缝：`AskPermission` 类型 + 最简策略 + `check()` | 不认识循环，不知道答案从哪来 |
| `agent.py` | **内核：循环本体** | 只依赖上述模块的接口 |
| `cli.py` | 最笨的客户端 | 只消费事件 + 实现 `ask_permission` |

**依赖方向单向向下，无回环**：

```
cli → agent → {llm, tools, permission} → events
cli → config ← llm
```

即：`agent.py` 既不读配置也不读环境，它只接收构造好的 `llm`、`tools`、`ask_permission`。因此每一层都可独立测试，内核无需任何环境准备。

`permission.py` 单独成文件（v1 仅约 15 行）是刻意的：**策略与机制必须分家**。以后把「终端 y/n」换成「规则文件」或「手机远程确认」，改的是这个文件，`agent.py` 一行不动。

### 配置项

```
NORMA_BASE_URL=https://api.deepseek.com
NORMA_API_KEY=sk-...
NORMA_MODEL=deepseek-chat
NORMA_MAX_STEPS=25
```

## 7. 事件协议

五条，一条不多：

```python
@dataclass
class TextDelta:        # 模型正在吐字
    text: str

@dataclass
class ToolCallStarted:  # 模型发起的工具调用（参数已拼完）；必定配对一条 ToolResult
    call_id: str
    name: str
    args: dict

@dataclass
class ToolResult:       # 执行完毕；content 是回填给模型的原文
    call_id: str
    name: str
    ok: bool
    content: str

@dataclass
class Finished:         # 模型不再调用工具，循环正常结束
    text: str

@dataclass
class Failed:           # 循环无法继续（API 失败、超步数）
    reason: str
```

### 设计决定

**1. 没有 `PermissionRequested` 事件。**

权限询问走注入的 `ask_permission` 可调用对象，不走事件流。理由：事件是**单向广播**，权限是**需要回话的**。客户端本来就必须实现「回应通道」这第二个接入点；与其在事件流里加一条、再另开一条回程通道，不如把「问一句并等答案」直接做成一个函数。少一个概念，少一处状态同步。

客户端若想在等待时渲染「等待授权中…」，就在自己的 `ask_permission` 实现里先渲染再 await。**UI 状态是客户端的事，不是协议的事。**

**2. `ToolCallStarted` 与 `ToolResult` 严格一一配对。**

模型发出的每一次工具调用都会产生一条 `ToolCallStarted`，随后**必定**有一条同 `call_id` 的 `ToolResult`——包括工具名不存在、参数非法、被权限拒绝这些失败情况。

反过来的设计（只在确定要执行时才发 `ToolCallStarted`）会产生**孤立的 `ToolResult`**：客户端收到一条指向它从未见过的 `call_id` 的结果，被迫处理「未知调用的结果」这种状态。配对不变式让客户端只需维护一张 `call_id → 卡片` 的表。

代价是客户端会看到模型拼错的调用。但那正是**真相**——助手确实尝试了，UI 应该显示它失败了，而不是假装什么都没发生。

**3. 预期内失败一律是 `Failed` 事件，不是异常。**

规则：

- API 超时/失败、超步数、鉴权失败 → `yield Failed` 然后 `return`
- 代码 bug（如工具名重复注册）→ 直接抛

客户端只需处理一种错误路径；调试时不会把 bug 误判为网络抖动。

**4. `ToolResult.ok` 与 `content` 并存。**

`content` 是回填给模型的原文，可能很长（已截断，见 §8）。客户端展示时自行决定截断，内核不替它决定。

**5. `Finished.text` 是重复信息，渲染以 `TextDelta` 为准。**

正文已随 `TextDelta` 流式送达，`Finished.text` 与最后一轮 deltas 的拼接结果相同。
保留该字段是为了让内核测试能在不消费事件流的前提下断言最终答案；
**客户端渲染正文一律用 `TextDelta`，忽略 `Finished.text`。**

把这条写进协议而不是留在 `cli.py` 的注释里：三个客户端各自猜一遍，"答案打印两次"
是必然结果。

### 序列化

`Event` 基类提供约 2 行的 `to_dict()`（`dataclasses.asdict` + 类型名）。这条协议的全部意义就是将来被三个语言的客户端消费，所以配一条测试断言每种事件都能 `json.dumps`——它防的是「某天有人往事件里塞了个 `Path` 对象」这类直到接手机端才炸的错。

## 8. 工具

### 形态

```python
class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    EXEC = "exec"

@dataclass
class Tool:
    name: str
    description: str
    params: type[BaseModel]                     # pydantic：既校验参数，又生成 schema
    risk: Risk
    fn: Callable[..., Awaitable[str]]
```

模型所需的 tool 定义零手写：

```python
def openai_schema(t: Tool) -> dict:
    return {"type": "function", "function": {
        "name": t.name,
        "description": t.description,
        "parameters": t.params.model_json_schema()}}
```

注册表是 `tools.py` 里一个模块级 `TOOLS: dict[str, Tool]`。**不加装饰器、不加插件机制**——尚未到需要它的规模。

加一个工具 = 定义 pydantic 模型 + 写 async 函数 + 在 `TOOLS` 里加一行。

### v1 的四个工具

| 工具 | 风险 | 说明 |
|---|---|---|
| `list_dir(path=".")` | read | 结构化列目录。看似可省，但模型极度依赖它定位；用 shell 代替效果明显变差 |
| `read_file(path)` | read | 读文本。**只能从文件开头读**，出口上限见下 |
| `write_file(path, content)` | write | 写/覆盖，自动创建父目录 |
| `run_powershell(command, timeout_s=60)` | exec | Windows 上的主力，也是最大的风险面 |

移动/复制/删除**不单独做工具**——`run_powershell` 已覆盖，且同属 write/exec 风险等级，多一个工具只多一处要维护的描述文案。

**`read_file` 曾经有第三个参数 `max_bytes`，v1.0.1 删掉了。** 它是个假旋钮：真正的闸门是下面那层 8000 字符的结果截断，而它**在 `max_bytes` 之后**才生效，于是对中文（3 字节/字）`max_bytes` 超过约 24000 就完全无效。实测模型为此试了 6万/20万/40万字节、返回一模一样，白烧数步。现在它只有一个**内部**常量 `_READ_CAP_BYTES = 64000`，负责把文件按字节切开（那是"末尾半个字符是不是我们自己切的"这个判断的前提），超出时会在结果里注明文件真实大小。**区间读取（`offset`/`limit`）是 1.1.0 的事**——见 `docs/superpowers/plans/2026-09-16-v1.0.1-design.md` §10。

### 截断

工具输出会原样进入模型上下文。`dir /s` 一次能吐数 MB 直接撑爆窗口。**每个工具结果硬上限 8000 字符**，超出截断并标注。这是 `tools.py` 的职责，不是协议层的。

**这 8000 字符是唯一跟模型见面的闸门**，所以它必须写在工具描述里让模型知道——否则模型只能靠试参数去发现，而"试参数"这个行为本身就是被 `max_bytes` 骗出来的。<br>
配套的一句：截断标记里的"原文 N 字符"报的是**工具返回的那段文本**的长度。内部分段读（如 `read_file`）必须自己再标一句文件真实大小，否则模型会把 `N` 当成整个文件的长度。

## 9. 执行路径（本设计的核心）

每次工具调用依次过六步。**每一条失败路径都变成结果回填，没有一条会中断循环**：

```python
@dataclass
class ExecResult:          # 内部类型，不是协议类型——勿与协议事件 ToolResult 混淆
    ok: bool
    content: str

async def execute(self, call: ToolCall) -> ExecResult:
    result = await self._run(call)
    audit(call.name, call.args, result.ok, result.content)   # 每个调用都记一行，含失败与拒绝
    return result

async def _run(self, call: ToolCall) -> ExecResult:
    tool = self.tools.get(call.name)                      # ⓪ 工具存在性
    if tool is None:
        return ExecResult(False, f"没有名为 {call.name} 的工具")

    if call.args_error:                                   # ① arguments 是否合法 JSON
        return ExecResult(False, f"参数错误：{call.args_error}")

    try:                                                  # ② 权限
        denied = await permission.check(tool, call.args, self.ask_permission)
    except Exception as exc:
        denied = f"权限检查失败，已按拒绝处理：{exc}"
    if denied:
        return ExecResult(False, denied)                  #    见 §10 的拒绝文案

    try:
        validated = tool.params(**call.args)              # ③ 参数校验
    except ValidationError as e:
        return ExecResult(False, f"参数错误：{e}")

    try:
        out = await tool.fn(**validated.model_dump())     # ④ 执行
    except Exception as e:
        return ExecResult(False, f"工具报错：{e}")

    return ExecResult(True, truncate(out))                # ⑤ 截断
```

**⓪ 不是凑数的。** 模型会幻觉出不存在的工具名——它会调用 `move_file`、`search_web` 这类它认为「应该有」的工具。若不拦截，`TOOLS[name]` 直接 `KeyError` 崩掉整个 run。必须把它当成常规路径而非异常。

**① 同样不是凑数的。** 流式响应被 `max_tokens` 截断时，`arguments` 会断在半截，`json.loads` 必然失败。仅靠 ③ 的 pydantic 校验看似能挡住——**但只对必填参数的工具成立**。一个参数全可选的工具会带着空 `{}` 静默执行，这是会造成错误执行的风险，不能依赖「我们的工具恰好都有必填参数」这种巧合。因此参数在 `llm.py` 解析失败时记录 `ToolCall.args_error`，在这里率先拦截。

顺序说明：① 在 ② 之前，因为参数都不成立时没有询问用户的必要——不该让用户去批准一次注定失败的调用。

**② 自身也要兜住，且默认拒绝。** 权限检查本身可能失败：远端 `ask` 的网络错误、CLI 在询问时读到 `EOFError`。这类失败若直接抛出会毁掉整个 run，而默认放行更危险。因此按 **fail-closed** 处理——检查失败一律视为拒绝，理由回填给模型。

`ask_permission` 是构造 `Agent` 的**必填参数**，不给默认值。缺参数应当在你写代码时立刻报错，而不是等到第一次写文件时才炸。

**`ExecResult` 与协议事件 `ToolResult` 是两个东西**：前者是执行层内部产物，后者是发给客户端的播报（含 `call_id`、`name`）。执行层不该知道事件的存在，`agent.run` 负责把前者包装成后者。

`execute` 包一层 `_run` 是为了保证**审计一定落盘**——每条返回路径都要记日志，散在各个 `return` 前迟早漏一个。

模块归属：`execute` 是 `Agent` 的方法（`agent.py`）；`audit` 与 `truncate` 是 `tools.py` 里的辅助函数。

**「面向用户的报错文案用中文」管的是我们自己写的文案，不是第三方诊断的原文。** 因此：

- 前缀、拒绝理由、引导语由我们撰写 → 必须中文（`参数错误：`、`工具报错：`、`权限检查失败，已按拒绝处理：`、`没有名为 X 的工具`，以及 §10 那条拒绝文案）
- `{e}` 引用的 pydantic `ValidationError` 与工具异常文本 → **保持原文**，这是被引用的证据而非原创文案

理由不是省事，是两条实打实的收益：模型需要 `text: Input should be a valid string` 这类精确定位才能自纠；而翻译 pydantic 的错误分类意味着我们要永久维护一张跟随上游版本漂移的映射表。刻意保留中文前缀 + 英文诊断载荷，是权衡后的选择。

**这是 agent 能自愈的全部秘密。** 模型看到「参数错误」会重试，看到「用户拒绝了」会换个办法，看到「没有这个工具」会改用现有的。若此处任何一处 `raise` 出去，一次拼错字段名就整个 run 崩掉——而这在真实使用中是每天都会发生的事。

> **回填文案是提示工程的一部分，不是日志文案。** v1.0.1 之前这条拒绝理由写的是「用户拒绝了这次操作」，
> 实测被模型读成「PowerShell 在这台机器上不可用」，于是连试 4 种命令、最后拿 `echo test` 试探环境。
> 教训：**任何回填给模型的失败理由，都要写清"哪个调用、是不是环境坏了、接下来该试什么"**——
> 一句话少一个要素，就是几步的浪费。六个失败路径各有一条这样的文案，改它们等于改 agent 的行为。

## 10. 权限闸门

```python
AskPermission = Callable[[Tool, dict], Awaitable[bool]]

POLICY: dict[Risk, bool] = {          # True = 执行前必须问
    Risk.READ:  False,
    Risk.WRITE: True,
    Risk.EXEC:  True,
}

async def check(tool, args, ask) -> str | None:
    """None = 放行；字符串 = 拒绝理由（会回填给模型）"""
    if not POLICY[tool.risk] or await ask(tool, args):
        return None
    return (
        f"用户拒绝了这次 {tool.name} 调用。"
        f"这不是工具坏了或环境不可用，而是用户这一次不允许。"
        f"请改用其他工具或换个思路完成任务。"
    )
```

契约一句话：**`None` 放行，字符串是拒绝理由**。不引入 `Decision` 类，不引入策略引擎。

**拒绝理由必须指名工具并给出去处**（v1.0.1 改的）。三个要素缺一不可，理由见 §9 那条引用块：模型需要知道"拒绝的确切对象是这一次调用""这不是环境故障""接下来该试什么"。写成泛泛的「这次操作」时，它会把拒绝理解成环境不可用。**注意这里不承诺"再问一次可能过"**——那是 `POLICY` 这张表的事，`check()` 不替策略做承诺。

**`check()` 拿到的 `bool` 分不清"用户说不"与"`ask` 自己坏了"，也不该分。** 后者（EOF、远端断线）在 §9 的 ② 按 fail-closed 单独处理，文案是 `权限检查失败，已按拒绝处理：{exc}`——前缀不同、且带着异常原文，因此与真拒绝可区分。**要不要给那条也补上"换个工具"的引导，是 v2 设计权限往返时的事**：v1 里 `ask_in_terminal` 自己就把 EOF 吞成了 `False`，所以那条路径实际上很少被走到。

**关于取值的设计意图**：缝留好、策略最简。v1 在用户自己的电脑上、对着本地终端使用，风险低；真正的风险从「能被远程唤起」那一刻开始指数上升。但闸门本身是循环的必经之路，属于内核，必须现在就在。以后换规则文件或远程确认，改的是这个函数和它上面那张表。

**这张表的最终形态是「声明式数据 + 本地覆盖」。** v1 硬编码（三个工具，为读文件写策略没有意义），但必须清楚它迟早变成读一份本地 YAML/JSON，且用户能在本地覆盖默认值。原因不是灵活，是安全性：权限边界必须**本地可读、可审计、可修改**。任何形式的「云端返回 allow/deny」都是错的设计（见 §16）。

`ask` 的两个实现（说明缝的价值）：

```python
# v1：终端
async def ask_in_terminal(tool, args) -> bool:
    answer = await asyncio.to_thread(
        input, f"要执行 {tool.name} {args}? (y/n) ")
    return answer.strip().lower() == "y"

# 将来：远程（agent.py 一行不改）
async def ask_over_websocket(tool, args) -> bool:
    await ws.send(json.dumps({"type": "permission_request",
                              "tool": tool.name, "args": args}))
    reply = await ws.recv()
    return json.loads(reply)["allow"]
```

## 11. 内核循环

```python
async def run(self, user_input: str) -> AsyncIterator[Event]:
    self.messages.append({"role": "user", "content": user_input})
    for _ in range(self.max_steps):
        reply = None
        async for item in self.llm.chat(self.messages, self.tools_schema):
            if isinstance(item, TextDelta):
                yield item
            else:
                reply = item                       # Reply（含 content + tool_calls）
        if reply is None:
            yield Failed("模型没有返回任何内容")
            return

        self.messages.append(reply.to_message())   # assistant 消息（含 tool_calls）
        if not reply.tool_calls:
            yield Finished(reply.content)
            return

        for call in reply.tool_calls:
            yield ToolCallStarted(call.id, call.name, call.args)
            result = await self.execute(call)
            yield ToolResult(call.id, call.name, result.ok, result.content)
            self.messages.append({                 # tool 结果回填
                "role": "tool",
                "tool_call_id": call.id,
                "content": result.content,
            })

    yield Failed(f"超过最大步数 {self.max_steps}")
```

要点：

- `max_steps` 计的是**模型回合数**，不是工具调用数。默认 25。
- 每轮必须把 assistant 消息（含 `tool_calls`）追加进 `messages`，否则模型下一轮会重复调用同一工具。
- 六条已知失败路径（工具不存在、arguments 非法 JSON、权限拒绝、权限检查失败、参数校验失败、工具报错）均在 `execute` 内转为结果回填，不中断循环（见 §9）。
- 每个 `ToolCallStarted` 必定配对一条 `ToolResult`（见 §7 决定 2）。
- **多工具串行执行**。OpenAI 兼容接口可以一次返回多个 `tool_calls`，v1 串行处理：顺序确定，权限询问不会同时弹两个。这是一处刻意简化：

```python
# ponytail: 多工具串行执行；若并发成为可测量瓶颈，改为 asyncio.gather，
#           并注意权限询问需排队，否则会同时弹出多个确认框
```

### system prompt（v1.0.1 补上）

`messages[0]` 是一条 system 消息，由**内核**提供默认值（`DEFAULT_SYSTEM_PROMPT`），调用方可用 `Agent(system_prompt=...)` 覆盖。

```python
Agent(llm, ask_permission, tools=None, max_steps=25, system_prompt=None)
#   None = 用内核默认    "" = 明确不要 system 消息
```

- **默认值必须在核心里**，不能在客户端：放客户端的话，下一个客户端（v2 的浏览器端）会再漏一次，而"中文提问先用英文答"的缺陷就原样复现。
- **只插一次**，判据是"`messages` 里本来就有一条 system 消息"，而不是另记一个布尔量——这样客户端自己往 `messages` 里塞了东西（比如将来加载历史）不会被覆盖，也不必维护第二份状态。
- **内容是提示工程，不是产品文案。** 每句治一个观察到的毛病：语种、工具选择（别拿 `run_powershell` 去读文件）、读取上限（8000 字符，别再试参数）、以及"绝不编造"。**不写**工具清单（schema 已经给了）与客套话。
- 它跟着 `messages` 每轮重放，因此**插入次数是上下文成本的一部分**：插两次就是每轮多付一次。

### LLM 适配层

`llm.chat()` 是 async generator，先吐若干 `TextDelta`，最后吐一个 `Reply`：

```python
@dataclass
class ToolCall:
    id: str
    name: str
    args: dict          # 已解析的 dict；解析失败时为空 dict
    args_error: str = ""    # 非空 = arguments 不是合法 JSON（见 §9 的 ①）

@dataclass
class Reply:
    content: str
    tool_calls: list[ToolCall]

    def to_message(self) -> dict:
        return {"role": "assistant", "content": self.content or None,
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.name,
                                             "arguments": json.dumps(c.args, ensure_ascii=False)}}
                               for c in self.tool_calls]}
```

**`arguments` 回填时必须序列化成 JSON 字符串，不能是 dict。** 这是常见的静默错误：部分服务端会宽容接受，直到某天换模型或参数含特殊字符时才报错，届时很难定位。

**tool_call 增量拼接是本实现中最容易写错的地方。** OpenAI 兼容的流式接口把 `tool_calls[].function.arguments` 拆成**字符串碎片**，按 `index` 分片下发。必须按 index 累积，全部结束后才 `json.loads`。

它平时不暴露：单个短参数调用常常一个 chunk 就到齐了；等参数变长、或模型一次调两个工具时才崩——那时你已经在调别的东西了。因此必须有独立单元测试（见 §13 第 8 条）。

## 12. 审计日志

每个工具调用记一行到 `~/.norma/audit.log`：工具名、参数、放行与否、结果前 200 字。用 stdlib `logging`，约 5 行。

**位置是固定的，绝不写进 CWD**（v1.0.1 改的）：写在 CWD 会污染用户的工作目录，而且日志文件会出现在 `list_dir` 的结果里、被模型当成项目内容读掉（实测白烧一步）。用 `~/.norma/` 而不是 Windows 专有的 `%APPDATA%`——两个平台同一条路径，代码里不必有平台分支。`NORMA_LOG_PATH` 是测试与调试用的逃生舱，不是用户配置项。

**这不是可选项**：一个会在你机器上执行命令的助手，用户必须能事后查它到底干了什么。这也是以后做远程接入时的第一道取证手段。

## 13. 测试策略

测试按模块分文件。接缝在 `llm.py`——它是唯一碰网络的地方。

| 文件 | 覆盖 |
|---|---|
| `test_events.py` | 5 种事件的 `to_dict()` 结构；全部可 `json.dumps`（防 `Path` 之类非原生类型混入） |
| `test_config.py` | 缺 `NORMA_API_KEY` 时报错清晰；默认值正确 |
| `test_llm.py` | **tool_call 增量拼接**（见下，全套重点） |
| `test_tools.py` | 各工具行为（`tmp_path`）；截断；超时杀进程；中文输出编码 |
| `test_permission.py` | read 不问；write/exec 询问后被允许 / 被拒绝 |
| `test_agent.py` | 循环的 11 种情形（见下） |

`test_agent.py` 用照剧本走的假 LLM——它是唯一的注入点：

```python
class FakeLLM:
    def __init__(self, script): self.script = list(script)   # 每轮的预设输出
    async def chat(self, messages, tools):
        for item in self.script.pop(0):
            yield item
```

| # | 验证 |
|---|---|
| 1 | 模型不调工具 → `Finished` |
| 2 | 调一次工具 → 结果真的回填进 `messages` → `Finished` |
| 3 | 工具抛异常 → 错误回填 → 模型恢复 |
| 4 | 权限被拒 → 拒绝理由回填 → 模型改道 |
| 5 | 参数不合法 → 校验错误回填（**不崩**） |
| 6 | 工具名不存在 → 错误回填（**不崩**，§9 的 ⓪） |
| 7 | `arguments` 非法 JSON → 错误回填（**不崩**，§9 的 ①） |
| 8 | 超过 `max_steps` → `Failed` |
| 9 | **`ToolCallStarted` 与 `ToolResult` 严格配对**（§7 决定 2 的不变式） |
| 10 | read 类工具不触发权限询问 |
| 11 | 权限检查自身抛错 → **fail-closed**，按拒绝回填（**不崩**） |

### `test_llm.py`：全套最重要的一组

理由见 §11。必须覆盖：

- 纯文本流 → 若干 `TextDelta` + 一个无 tool_call 的 `Reply`
- 单个工具调用，`arguments` 在**多个 chunk 里分片**到达
- 两个工具调用按 `index` **交错**到达
- 首个 chunk 之后 `id` / `function.name` 为 `None`（真实 API 行为；写成赋值而非累加会把已取到的工具名清空）
- `arguments` 被截断成非法 JSON → `args_error` 非空
- 空 `choices` 的收尾 chunk（带 usage）被安全忽略

### 手工冒烟

真 API 跑一次「列出我的下载文件夹」。这是唯一能验证「模型真的会调工具」的检查——假 LLM 只验证循环，不验证与真实模型的协议是否对得上。

## 14. 已知取舍

| 取舍 | 代价 | 升级路径 |
|---|---|---|
| 无 `PermissionRequested` 事件 | 客户端有两个接入点（事件流 + ask 实现） | 若需把「等待授权」做成一等协议事件，改为双向流 |
| 会话历史仅存内存 | 退出进程即失忆 | 加 JSONL 追加写 + 启动加载 |
| 无 token 统计 | 不知道花了多少钱 | 在 `Reply` 上带 `usage`，累计到会话 |
| 多工具串行 | 多工具场景变慢 | `asyncio.gather` + 权限询问排队 |
| 工具结果截断 8000 字符 | 超大输出会丢信息；**它同时也是读文件的唯一出口**，所以 `read_file` 一次最多给出 8000 字符 | 改为写临时文件、只回填路径与摘要；或给 `read_file` 加区间读取（1.1.0） |
| 权限策略硬编码在 `permission.py` | 改策略要改代码 | 规则文件 / 远端确认 |
| 无上下文压缩 | 长会话会触窗口上限 | 历史摘要 + 保留最近 N 轮 |

实现与审查阶段暴露出的天花板，一并记录（都不是缺陷，是**已知边界**）：

| 取舍 | 代价 | 升级路径 |
|---|---|---|
| `run_powershell` 超时只杀**直接**子进程 | 模型若派生脱离的孙进程（`Start-Process`、`cmd /c start`）且撞上超时，该进程会存活。日常的 `Get-ChildItem`/`Copy-Item`/`Invoke-WebRequest` 都是进程内 cmdlet，随父进程一起死 | 用 ctypes 加 Win32 Job Object（`CreateJobObject` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` + `AssignProcessToJobObject`），约 40 行平台专有代码；或限制该工具不派生脱离进程 |
| `POLICY[tool.risk]` 是裸下标 | 未来新增 `Risk` 成员而未同步补 POLICY 表，会在**该工具被用到时**抛 `KeyError`。方向是 fail-closed（永不误放行），且 §9 的 ② 守卫会把它转成「权限检查失败，已按拒绝处理」，循环不死 | 加一条不变量测试 `assert set(POLICY) == set(Risk)`，把运行期失败提前成改完立刻测试红；或改 `POLICY.get(tool.risk, True)` 让默认变为「询问」（仍 fail-closed，但给出中文理由而非 KeyError 文本） |
| `_child_env()` 只剔除 `NORMA_` 前缀 | 接入第二家 provider 时，它的密钥（如 `OPENAI_API_KEY`）仍会被每个子进程继承，而 §9 的 I3 修复正是为了堵这条泄露路径 | 改为白名单（只保留 PATH/SystemRoot/TEMP 等必需项），或按 provider 前缀扩展剔除列表 |
| `write_file` 非原子覆盖 | `write_text` 先截断再写；写入中途失败（磁盘满、ACL 变更、掉电）会两个版本都没了 | 写同目录临时文件，再 `os.replace`（Windows 上同样是原子的） |
| Windows 上 `\n` 被静默改写成 `\r\n` | text 模式默认 `newline=None`，故 write→read 往返非恒等；生成的 `.sh` 在 git-bash 下会坏。对 `.txt` 而言 CRLF 可能本就正确 | `write_text(..., newline="")` 若需要字节保真 |
| 系统代理会拦截指向 **localhost** 的请求 | `httpx2` 读 Windows 的 `ProxyServer`（Clash 等常驻）却**不认** `ProxyOverride` 里的 `127.*` 绕过规则，于是本地请求被塞给代理、换来 502（而非连接被拒，所以错误离病因很远）。今天不影响 `api.deepseek.com`；**一旦把 `NORMA_BASE_URL` 指向本地模型（Ollama 之类）就会踩到** | 给 `LLM` 加可配置的 `http_client`，或在 `base_url` 落在 localhost 时自动绕过代理。测试侧已显式设 `NO_PROXY` 隔离（见 `tests/test_cli.py`） |

v1.0.1 这一轮新增的边界：

| 取舍 | 代价 | 升级路径 |
|---|---|---|
| `read_file` 只能从头读（无 `offset`/`limit`） | 读不到任何非开头部分。**实测这是小说场景的硬阻塞**：16848 字符的文件，模型调 3 次 `read_file`（3×8000 够覆盖全文）却只读到开头一半——因为每次都从头开始，重试同一个工具永远在原地打转 | 1.1.0 加 `offset`/`limit`；设计输入见 `docs/superpowers/plans/2026-09-16-v1.0.1-design.md` §10（按**字符**不按字节、返回值带进度、上限先别做成参数） |
| `read_file` 内部 64 KiB 上限（不可调） | 超过 64 KiB 的纯文本尾部读不到（此前也读不到，因为出口只有 8000 字符）。**现在至少会说清楚**（`…[文件共 N 字节，这里只读了开头 M 字节]`），不会让模型把"原文 64000 字符"当成文件大小 | 与 `offset` 同批解决；或改流式解码 |
| 日志固定 `~/.norma/audit.log`，无轮转 | 长期使用会持续增长 | 接入 `RotatingFileHandler`，或客户端提供查看入口 |
| `setup_logging()` 不防重复调用 | 调两次会挂两个 handler、同一行写两遍，前一个句柄不关（真实入口只调一次，测试自己收尾） | 一行 `any(isinstance(h, FileHandler) and Path(h.baseFilename) == path …)` 早退 |
| 日志目标不可用时抛裸 traceback | 目录被占用、父路径是文件时启动即崩，缺 `配置错误：` 那种友好文案 | 与 `Config.from_env()` 的错误处理对齐 |
| `system_prompt` 内容是硬编码中文 | 想换语种或换人格要改代码，或在构造 `Agent` 时传入 | 规则文件式配置（与权限策略同一批） |
| 拒绝文案不承诺"再问一次可能过" | 模型不会主动重试（**这正是想要的**） | v2 设计权限往返时，把"可重试"做成策略层的信息 |
| fail-closed 那条（`权限检查失败，已按拒绝处理：{exc}`）没有"换个工具"的引导 | 与真拒绝相比少了诊断性——但前缀不同、且带异常原文，仍可区分；v1 里 `ask_in_terminal` 先把 EOF 吞成 `False`，这条路径很少走到 | v2 权限往返设计时一并对齐 §10 那条三段式 |

## 15. 环境前提

**本机当前未安装 Python。**

`where python` 只命中 `C:\Users\Administrator\AppData\Local\Microsoft\WindowsApps\python.exe`，这是一个 **0 字节的 Microsoft Store 别名桩**，运行返回 `9009`（命令未找到）。系统内无 python.org 安装、无 conda、无 uv、无 pip。

实现前需先安装 Python 3.11+（推荐 python.org 官方安装包，或 `winget install Python.Python.3.12`），并确认该 Store 别名不再抢占 `PATH`（设置 → 应用 → 高级应用设置 → 应用执行别名，关闭 `python.exe` / `python3.exe`）。

## 16. 分发形态与演进（记录，不在 v1 范围）

「将来给其他人一起使用」时的边界。**其中两条约束 v1 的实现纪律**，故记录在案。

### 仓库边界 ≠ 安装边界

**内核独立成仓，不等于用户要装两次。** 打包时内核作为 **sidecar（随行进程）** 嵌入客户端安装包：

```
norma 仓（内核）
   └─发布─→ norma-kernel-0.1.0-win-x64.exe     ← PyInstaller 冻结的独立可执行文件
                     │
Electron 仓 ─构建时下载并锁定版本─┘
                     │
                     └─ electron-builder 的 extraResources 打进安装包
                                 │
                   用户装一份安装包 ──→ 内核作为子进程被拉起
```

用户从头到尾看不到 Python，也不需要单独安装内核。

冻结内核的三种做法：

| 做法 | 说明 | 取舍 |
|---|---|---|
| **PyInstaller / Nuitka 冻结** | 内核编译成独立 `.exe` | **推荐**。最简单，`extraResources` 直接打包 |
| 内嵌 Python（python-embed） | 随包带解释器与依赖 | 文件多，但没有冻结带来的怪问题 |
| 要求用户自装 Python | ❌ | 没人会为了用个助手去装 Python |

选 PyInstaller 的理由：依赖只有 `openai` / `pydantic` / `python-dotenv`，全是纯 Python 或带预编译 wheel 的，冻结难度低。已知小摩擦：`pydantic-core` 是 Rust 扩展，PyInstaller 偶尔需要 `--collect-all pydantic_core`。这也是 §5 选 `src/` 布局的第二个理由——冻结工具对包发现敏感。

这是标准实践，不是自创：Ollama、LM Studio、Docker Desktop、VS Code 都是「一份安装包里塞运行时」。

### 为什么内核仍然必须独立成仓

仓库边界跟随**依赖方向**，不是安装边界：

```
        ┌─→ Electron 仓 ─┐
内核仓 ─┼─→ Android 仓  ─┼─→ 三者都依赖内核，内核不认识任何一个
        └─→ 鸿蒙仓      ─┘
```

- 三个客户端依赖一个内核 → 内核必须能**独立版本化、独立测试、独立发版**；否则手机端修个 bug 要动桌面端的发布流程
- **Android 与鸿蒙客户端无法从 Electron 仓构建**。内核若住在 Electron 仓里，手机端将依赖一个桌面应用仓库——荒谬
- 内核是唯一承担安全责任的部分，需要自己的测试与发布纪律

### 内核的存活不得绑定在 Electron 上

§1 已定：手机关掉桌面窗口也必须能连上内核。因此内核**不能**是随 Electron 退出而死的子进程。

做法：Electron 以 **detached** 方式拉起内核 + 注册开机自启（或直接注册为 Windows 服务）。Electron 只是连接方与守护方，不是内核的宿主。

属于传输层里程碑，v1 不涉及，但记录在案以免打包时才发现。

### Tool 与 Skill 不是一回事

| | 是什么 | 可否集中分发 |
|---|---|---|
| **Tool** | Python 代码 | **否**——分发代码等于分发可执行程序，需签名、审核、沙箱 |
| **Skill** | 纯数据（提示词 / 流程 / 用哪些工具） | **是**，零执行风险 |

Skill 是数据，一个 git 仓库或静态 URL 即可分发，**不需要服务**。Tool 是代码，且不该集中分发。

### 什么才真需要服务

| 东西 | 小规模（几个朋友） | 何时才真需要服务 |
|---|---|---|
| Skill 分发 | git 仓库 / 静态 URL | 需要灰度、审核时 |
| 权限策略模板 | 静态文件 + 本地覆盖 | 基本永远不需要 |
| 远程接入（手机 → 家里电脑） | **Tailscale 等组网工具** | 用户多到不能要求安装第三方 |
| 账号 / 授权 | 不需要，各实例独立 | 需要计费或权限管控时 |
| 更新分发 | 检查 release 版本号 | 需要强制升级时 |
| 遥测 | 不需要 | 想知道别人怎么用时 |

**小规模的正确服务数量是零。** 唯一技术上真麻烦的是 NAT 穿透（手机在 4G 上连家里电脑），而它由组网工具解决，不必自建中继。等真有几十上百用户再谈控制面。

### 不可协商：权限判断永远在本地

云端**可以**分发默认策略模板，**最终判断必须在本地内核**：

1. 断网不应导致助手瘫痪——本地执行本该离线可用
2. 云端被攻破 = 所有用户的机器同时失守
3. 用户必须能审计自己的安全边界；一个黑盒返回的 `allow: true` 与之不可比

Skill 同理：**分发可集中，执行必须在本地。**

### 「发安装包给别人」意味着什么

1. **内核必须能独立安装**（不是 dev 环境跑 Python 脚本）→ 需打包为可执行文件
2. **需要自更新机制**，否则你会永远在帮别人手动升级
3. **发布者成为安全责任主体**：该程序将在他人机器上执行任意命令。这不是技术问题，是真实责任——也正是权限闸门必须从 v1 就在的原因
4. 默认**各实例独立、互不相通**：他人的内核不得指挥你的机器

### 由此约束 v1 的两条纪律

> **① 内核必须能完全独立运行。** 不允许任何「启动时必须联网获取配置」的设计。网络是可选增强，不是依赖。
>
> **② 权限策略是声明式数据。** v1 硬编码（见 §10），但方向是读一份本地 YAML/JSON 并允许本地覆盖。

## 17. 下一里程碑（v2）：传输层与最小客户端

> 本节写给「接手 v2 的人（或下一个对话）」。读完本节 + §1 客户端拓扑 + §7 事件协议 + §14 已知取舍，即可开工，不必回头读 v1 的执行过程。

**决定：v2 做传输层 + 一个最小浏览器客户端。不做 Electron，不给内核补功能。**

### 为什么是这个顺序

事件协议（§7）**至今没有任何消费者**——它是靠推理设计的，不是靠使用。在第一个客户端接入之前，「协议够不够」这个问题无法回答，只能靠消费一次。

已经能预判的缺口（v1 终局审查发现）：`ToolCallStarted` 不带参数，所以 UI 上只能显示「调用了 list_dir」，显示不出「列出了哪个目录」。这类缺口只有真做一个客户端才会暴露。

### v2 要回答的三个问题（都是设计问题，不是实现问题）

1. **协议怎么上线** —— 事件序列化成 JSON 行？一个连接一个会话？
2. **权限怎么问回来** —— 这是传输层的硬骨头。v1 里 `ask_permission` 是个本地的 async 可调用对象；跨网络之后它变成「发出去一个问题 → 等回复 → 超时或断线一律按**拒绝**（fail-closed）」。§7 决定 1 已预告：要把「等待授权」做成一等协议事件，就得改成双向流。**只有做一个真需要弹确认框的客户端才能拍板。**
3. **会话归属** —— §1 那条「客户端可插拔、会话不是」目前只是约定（`cli.py` 碰巧守住了）。真接客户端会冒出一堆没定的问题：一个会话能同时连几个客户端？断线重连算不算同一个会话？重连后要不要把历史重放给它？

### 为什么不做 Electron

Electron 是大承诺：构建工具链、打包、把内核当 sidecar 拉起并守护、崩溃重启、开机自启。**在协议稳定之前付这笔钱是浪费**——会一边搭脚手架一边改协议。最省的客户端是**一个 HTML 文件**：零构建工具，一两天的事，却能验证上面三个问题。Electron 之后只是换个壳。

### 为什么现在不给内核补功能

持久化、上下文压缩、token 统计、更多工具、规则文件式权限——**每一个都是猜**。等真实使用数据出来再做，一个都跑不掉；现在做，就是给一个没人用过的系统加功能（理由同 §2 的「v1 不做」表）。

### 真实使用需求（记录，用于对齐 v2 的取舍）

- **主要用途不是办公文档，而是辅助游戏开发**（根据小说改编）。含义：要读大文件、要跑多步骤长任务、要写代码并执行构建。
  - **立刻会撞到的墙**：`read_file` 上限 64000 字节（约 21000 汉字），而一部小说远超此数——**它现在读不完一部小说**。这是 v2 之前就要面对的具体缺口。
  - 25 步上限 + 无上下文压缩，会在「读若干章 → 提炼设定 → 写代码」这类任务上很快暴露。
- **公司是内网，norma 从工位接不进去。** 所以近期真实场景是**在家用**，不是远程办公。
- **手机端出来前，从手机下指令很困难。** → 这恰是「最小浏览器客户端」的最强理由：**局域网可达的网页在手机上直接能用**，不必等原生客户端。要跨出局域网再接 Tailscale（§16），不自建中继。
- **想把 norma 分享给朋友试用并收集反馈。** 今天**唯一不越安全边界**的路径是：朋友克隆仓库、装自己的 Python 与**自己的 API key**、在**自己的机器**上跑一个独立实例。§16 已定「各实例独立、互不相通」——让朋友连到**你的**内核，等于他能指挥你的机器，那是权限模型要单独设计的事，不是加个开关就能给的。
  要让分享变成「下载一个安装包就能用」，需要 §16 的打包（PyInstaller 冻结），而打包与客户端绑定，排在 v2 之后。
- **朋友用的是 macOS，而 norma 是 Windows 优先写的。** 平台耦合面很小但很具体：**只有 `tools.py` 一处**——`_SHELL = shutil.which("pwsh") or shutil.which("powershell") or "powershell"` 在 macOS 上两个 `which` 都返回 `None`，降级到字面量 `"powershell"`，于是 `run_powershell` 抛 `FileNotFoundError`。走 agent 时会被 §9 的兜底转成「工具报错：…」回填（循环不死），但**该工具在 Mac 上等于废掉**；`tests/test_tools.py` 里 6 条 PowerShell 用例也会红。其余全部跨平台（`list_dir`/`read_file`/`write_file` 是纯 pathlib；`cli.py` 的编码修复在 macOS 上是无害空操作）。
  - **零代码缓解**：`brew install --cask powershell` 让 `which("pwsh")` 命中，EXEC 大概率可用（`[Console]::OutputEncoding` 在 macOS 的 pwsh 上是否可设**未经验证**，但即便抛异常也是 graceful 失败）。
  - **不要现在改**：没有 Mac 无法验证，而盲改平台路径正是 v1 里反复栽跟头的同一类错误。正确形态是 v2 的「按平台选 shell」（Windows → PowerShell，POSIX → `sh`），顺带把工具改名 `run_shell`。
  - 朋友那边的测试预期：**91 绿 / 6 红**（红的全是 PowerShell 用例），需要提前告知，否则会被当成代码坏了。

### v2 的第一条纪律

> **先把协议消费一次，再决定协议长什么样。** 在最小客户端跑通「对话 + 一次工具调用 + 一次权限确认」之前，不要动 §7 的事件定义。
