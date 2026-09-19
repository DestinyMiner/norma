# norma

个人 AI 助手基站的**内核**：把「用户输入 → 模型推理 → 工具调用 → 拦截校验 →
执行 → 结果回填 → 继续推理 → 最终答案」这条循环做对、做稳。

上层客户端（Electron / Android / 鸿蒙）在其他仓库实现，通过事件协议连接本内核。
本仓库不含任何 UI。

设计文档：`docs/superpowers/specs/2026-09-15-norma-agent-kernel-design.md`

## 安装

需要 Python 3.11 或更高。

```bash
python -m pip install -e ".[dev]"
```

> Windows 上若 `python` 提示"未找到"或弹出应用商店，说明命中的是 Microsoft Store 的
> 别名占位程序。去「设置 → 应用 → 高级应用设置 → 应用执行别名」关闭 `python.exe` /
> `python3.exe`，或改用 `py` 启动器。

## 配置

复制 `.env.example` 为 `.env` 并填入密钥：

```dotenv
NORMA_BASE_URL=https://api.deepseek.com
NORMA_API_KEY=sk-...
NORMA_MODEL=deepseek-chat
NORMA_MAX_STEPS=25
```

`NORMA_BASE_URL` 指向任何 OpenAI 兼容端点，换服务商只需改这里（**模型名见 `NORMA_MODEL`**）。

## 使用

```bash
norma
```

输入自然语言即可。读写文件与执行命令前会先询问（只读操作不询问）。
每次工具调用都记入 `~/.norma/audit.log`（启动时会打印完整路径）。

`read_file` 一次最多返回 8000 字符，但可以**指定区间**接着读（`offset`/`limit`，按字符算），
所以长文件能一段段读完——返回值开头的进度标记会告诉你读到哪了、后面还有没有。

## 测试

```bash
python -m pytest
```

测试无需网络与 API 密钥——LLM 是注入的假实现。

## 加一个工具

1. 定义 pydantic 参数模型
2. 写一个 async 函数，返回字符串
3. 在 `src/norma/tools.py` 的 `TOOLS` 里加一项，五项都要写全：
   `name` / `description` / `params` / `risk` / `fn`

模型的 tool 定义由 pydantic 模型自动生成，无需手写 JSON Schema。
工具结果统一在 `Agent` 那层截断到 8000 字符，单个工具不必自己管这件事。
