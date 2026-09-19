import logging

import pytest

from norma.tools import (
    MAX_RESULT_CHARS, TOOLS, Risk, Tool, decode_text, openai_schema, tools_schema,
    truncate,
)


# ---------- 机制 ----------

def test_openai_schema_shape():
    schema = openai_schema(TOOLS["list_dir"])
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "list_dir"
    assert schema["function"]["description"]
    assert schema["function"]["parameters"]["type"] == "object"


def test_tools_schema_covers_every_registered_tool():
    names = {s["function"]["name"] for s in tools_schema()}
    assert names == set(TOOLS)


def test_tools_schema_with_empty_registry_is_empty():
    """空注册表是合法输入，不得悄悄回落到全局 TOOLS。

    这条守的是 `registry is None` 与真值判断的区别：Agent(tools={}) 表示
    "没有工具"，若回落成全局注册表，模型会被展示它根本调不到的工具。
    """
    assert tools_schema({}) == []


def test_every_tool_params_model_has_a_schema():
    for name, tool in TOOLS.items():
        assert tool.params.model_json_schema(), name


def test_truncate_leaves_short_text_untouched():
    assert truncate("短") == "短"


def test_truncate_marks_long_text():
    out = truncate("x" * (MAX_RESULT_CHARS + 50))
    assert out.startswith("x" * 100)
    assert "已截断" in out
    assert "8050" in out


def test_audit_writes_to_norma_audit_logger(caplog):
    from norma.tools import audit
    with caplog.at_level(logging.INFO, logger="norma.audit"):
        audit("list_dir", {"path": "."}, True, "有 3 个文件")
    assert "list_dir" in caplog.text
    assert "有 3 个文件" in caplog.text


# ---------- list_dir ----------

async def test_list_dir_lists_files_and_dirs(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")

    out = await TOOLS["list_dir"].fn(path=str(tmp_path))

    assert "[目录] sub/" in out
    assert "[文件] a.txt" in out


async def test_list_dir_reports_empty(tmp_path):
    assert await TOOLS["list_dir"].fn(path=str(tmp_path)) == "（空目录）"


async def test_list_dir_missing_path(tmp_path):
    out = await TOOLS["list_dir"].fn(path=str(tmp_path / "nope"))
    assert "不存在" in out


# ---------- read_file ----------

async def test_read_file_roundtrip(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("你好世界", encoding="utf-8")
    assert await TOOLS["read_file"].fn(path=str(f)) == "你好世界"


async def test_read_file_has_no_max_bytes_parameter():
    """`max_bytes` 是假的：真正的闸门是 `truncate()` 的 8000 字符，而它在 `max_bytes`
    之后才生效。实测模型为这个旋钮试了 6万/20万/40万字节，返回一模一样。

    这条测试是防止它被"顺手加回来"——**参数表是模型唯一能看到的地方**，
    多一个骗人的参数就多一次骗人的机会。要恢复区间读取，正确的是 `offset`/`limit`
    （见 docs/superpowers/plans/2026-09-16-v1.0.1-design.md §10），不是字节数。
    """
    schema = openai_schema(TOOLS["read_file"])["function"]
    assert "max_bytes" not in schema["parameters"]["properties"]
    # 描述里必须写死真实上限，否则模型只能靠试参数去发现它
    assert "8000" in schema["description"]


async def test_read_file_missing(tmp_path):
    out = await TOOLS["read_file"].fn(path=str(tmp_path / "nope.txt"))
    assert "不存在" in out


async def test_read_file_binary_is_reported_not_crashed(tmp_path):
    f = tmp_path / "b.bin"
    f.write_bytes(b"\xff\xfe\x00\x01")
    out = await TOOLS["read_file"].fn(path=str(f))
    assert "无法按 UTF-8 解码" in out


async def test_read_file_reads_a_file_larger_than_the_result_limit(tmp_path):
    """内部 64 KiB 上限：读得动、不崩，且没被误报成二进制。

    这条钉住内部上限的**存在与量级**（而不是继续暴露一个假参数）。上限之外
    还有一层 8000 字符的结果截断在 `Agent` 里，两者都在时**决定模型看见多少的是后者**。
    """
    f = tmp_path / "big.txt"
    f.write_text("x" * 70000, encoding="utf-8")

    out = await TOOLS["read_file"].fn(path=str(f))

    assert "无法按 UTF-8 解码" not in out
    assert len(out) < 70000          # 上限真的在起作用
    assert len(out) > 60000


# ---------- decode_text（read_file 的解码回退，单独可测） ----------

def test_decode_text_cut_mid_character_drops_the_partial_character():
    """中文每字 3 字节，按字节切经常正好切在一个字中间。

    这种残片**是我们自己切的**，必须丢掉残字返回完整前缀，而不是把整份中文
    误报成二进制文件。（原实现靠传入小 `max_bytes` 构造这个输入；参数删掉后
    直接喂裸字节，覆盖不丢。）
    """
    data = "中文测试".encode("utf-8")
    assert decode_text(data[:10], truncated=True) == "中文测"   # 3 字 + 第 4 字的第 1 字节


def test_decode_text_reports_a_complete_binary_file():
    """反过来：没被截断就是文件本身坏，不能说成"可能切在字中间"而把二进制当文本返回。"""
    assert decode_text(b"abc\xff\xfe", truncated=False) is None


def test_decode_text_cut_inside_a_four_byte_character_needs_the_full_backoff():
    """一个字符最多 4 字节，所以切点离字符边界最多 3 字节；只退 1 或 2 的实现会在这条上红。

    构造：末尾是 emoji（4 字节）的**第 1 个字节**。退 1 = `好` + emoji 的前 3 字节（仍非法）、
    退 2 = `好` + 前 2 字节（仍非法）、退 3 = 正好只剩 `好`（合法）。
    """
    data = "好".encode("utf-8") + "😀".encode("utf-8")[:1]
    assert decode_text(data, truncated=True) == "好"


@pytest.mark.parametrize(
    "cut,expected",
    [(4, "好"), (5, "好"), (6, "好"), (7, "好😀")],
)
def test_decode_text_never_reports_a_truncated_text_file_as_binary(cut, expected):
    """**每一个**可能的切点都要能救回来——这是中文文本不被误报成二进制的全部保证。

    固定退 1/2/3 字节在这里是够的（一个字符最多 4 字节，切点离边界最多 3 字节），
    但"够"要靠枚举证明，不能靠推理：这四条覆盖了 4 字节字符的每一个切点。
    """
    data = "好😀".encode("utf-8")[:cut]
    assert decode_text(data, truncated=True) == expected


# ---------- write_file ----------

async def test_write_file_creates_parent_directories(tmp_path):
    target = tmp_path / "a" / "b" / "c.txt"
    out = await TOOLS["write_file"].fn(path=str(target), content="内容")
    assert target.read_text(encoding="utf-8") == "内容"
    assert "已写入" in out


async def test_write_file_overwrites(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("旧", encoding="utf-8")
    await TOOLS["write_file"].fn(path=str(target), content="新")
    assert target.read_text(encoding="utf-8") == "新"


async def test_write_file_rejects_a_directory_path(tmp_path):
    target = tmp_path / "adir"
    target.mkdir()
    out = await TOOLS["write_file"].fn(path=str(target), content="x")
    assert out.startswith("不是文件")
    assert target.is_dir()  # 目录没有被破坏


async def test_write_file_reports_oserror_instead_of_raising(tmp_path):
    """父路径撞上已存在的文件：返回中文说明，而不是抛异常。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我是文件", encoding="utf-8")
    out = await TOOLS["write_file"].fn(path=str(blocker / "x.txt"), content="y")
    assert "写入失败" in out


# ---------- run_powershell ----------

async def test_run_powershell_captures_stdout():
    out = await TOOLS["run_powershell"].fn(command="Write-Output hello")
    assert "hello" in out


async def test_run_powershell_handles_chinese_output():
    """中文 Windows 上 PowerShell 默认输出编码不是 UTF-8，会糊成乱码。"""
    out = await TOOLS["run_powershell"].fn(command='Write-Output "中文测试"')
    assert "中文测试" in out


async def test_run_powershell_reports_nonzero_exit_code():
    out = await TOOLS["run_powershell"].fn(command="exit 3")
    assert "退出码 3" in out


async def test_run_powershell_captures_stderr():
    out = await TOOLS["run_powershell"].fn(
        command='[Console]::Error.WriteLine("出错了")')
    assert "出错了" in out


async def test_run_powershell_timeout_kills_process():
    out = await TOOLS["run_powershell"].fn(
        command="Start-Sleep -Seconds 30", timeout_s=1)
    assert "超时" in out


async def test_run_powershell_does_not_inherit_the_api_key(monkeypatch):
    """子进程环境里不能有 NORMA_*：否则一句 env: 就能把密钥打进日志与会话。"""
    monkeypatch.setenv("NORMA_API_KEY", "sk-secret-must-not-leak")

    out = await TOOLS["run_powershell"].fn(
        command='Write-Output "key=[$env:NORMA_API_KEY]"')

    assert "sk-secret-must-not-leak" not in out
    assert "key=[]" in out
