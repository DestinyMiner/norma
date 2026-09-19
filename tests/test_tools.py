import logging

import pytest

from norma.tools import (
    MAX_RESULT_CHARS, TOOLS, Risk, Tool, _decode_or_none, openai_schema,
    tools_schema, truncate,
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


def test_read_file_has_no_max_bytes_parameter():
    """`max_bytes` 是假的（v1.0.1 删掉了）：现在只有 `path`/`offset`/`limit` 三个参数。

    这条测试是防止它被"顺手加回来"——**参数表是模型唯一能看到的地方**，
    多一个骗人的参数就多一次骗人的机会。区间读取的正确形态是**字符游标**
    （`offset`/`limit`），不是字节数。
    """
    schema = openai_schema(TOOLS["read_file"])["function"]
    assert "max_bytes" not in schema["parameters"]["properties"]
    assert set(schema["parameters"]["properties"]) == {"path", "offset", "limit"}
    # 描述里必须写死真实上限，否则模型只能靠试参数去发现它
    assert "8000" in schema["description"]


def test_read_file_description_teaches_paging():
    """描述的每一半都治一个毛病，删掉任何一半都会把老问题放回来。

    实测教训：模型试了 `max_bytes` 的 6万/20万/40万，因为它**没有任何地方**能知道
    上限在哪；接着它为一个 16848 字符的文件调了 3 次 `read_file` 却只读到开头一半，
    因为它**没有任何地方**能知道该怎么读第二段。所以描述必须同时说清：
      * 上限是多少（8000），没有调大它的参数——否则就是那次烧步数的重演
      * 怎么翻页（offset 设成已读字符数、别重复读同一段）
    """
    description = openai_schema(TOOLS["read_file"])["function"]["description"]

    assert "8000" in description
    assert "offset" in description
    assert "不要重复读同一段" in description
    assert "没有能把上限调大的参数" in description


async def test_read_file_missing(tmp_path):
    out = await TOOLS["read_file"].fn(path=str(tmp_path / "nope.txt"))
    assert "不存在" in out


async def test_read_file_binary_is_reported_not_crashed(tmp_path):
    f = tmp_path / "b.bin"
    f.write_bytes(b"\xff\xfe\x00\x01")
    out = await TOOLS["read_file"].fn(path=str(f))
    assert "无法按 UTF-8 解码" in out


async def test_read_file_reads_a_file_larger_than_the_result_limit(tmp_path):
    """大文件读得动、不崩，且没被误报成二进制。

    注意它现在**返回的是一页**，而不是整个读到的 64000 字符：区间读取上了之后，
    "读进来多少"与"给出多少"仍然是两件事。这条钉住的是"大文件不崩、能给出第一页、
    并且说清文件比这大"。
    """
    f = tmp_path / "big.txt"
    f.write_text("x" * 70000, encoding="utf-8")

    out = await TOOLS["read_file"].fn(path=str(f))

    assert "无法按 UTF-8 解码" not in out
    assert len(out) <= MAX_RESULT_CHARS          # 整条结果不超硬上限
    assert out.startswith("…[第 1-")
    assert "70000 字节" in out                   # 文件比这一页大，说清楚了


async def test_read_file_says_when_it_only_read_the_head(tmp_path):
    """超过内部上限时必须自己标一句，**报文件真实大小**。

    不加这一句的话，页标记里的"共 N 字符"只是我们读进来的那一段的长度，
    模型会把它当成整个文件的长度——又是一次"工具说了不真的话"。
    （v1.0.1 的终局审查抓到过这个错的另一种形态：标记被 truncate 砍掉了。）
    """
    f = tmp_path / "huge.txt"
    f.write_text("x" * 70000, encoding="utf-8")

    out = await TOOLS["read_file"].fn(path=str(f))

    assert "70000 字节" in out          # 真实**字节**大小，不是读到的 64000
    assert "只读了开头" in out


async def test_read_file_does_not_mark_files_it_read_whole(tmp_path):
    """整个文件一次给完时**不加任何标记**。

    标记是用来回答"这是哪一段""后面还有没有"的——小文件没有这些疑问，
    给它套一层壳只会让模型每次读配置都要先跳过一句废话。
    """
    f = tmp_path / "small.txt"
    f.write_text("短短", encoding="utf-8")

    out = await TOOLS["read_file"].fn(path=str(f))

    assert out == "短短"


# ---------- read_file 区间读取（offset / limit） ----------

def _paged_file(tmp_path, n_chars=16848, name="novel.txt"):
    """造一个 16848 字符的中文文件——**实验里那个文件的真实规模**。

    内容每 100 字符带一个可定位的锚点（"00000-" 这类），好断言"我拿到的是哪一段"。

    用 `write_bytes` 而不是 `write_text`：Windows 上 text 模式会把 `\\n` 写成 `\\r\\n`，
    于是**读回来的字符数比写进去的多**，页数、偏移、总数全部跟着飘。
    """
    row = "".join(f"{i:05d}-" + "字" * 94 + "\n" for i in range(0, n_chars, 100))
    text = row[:n_chars]
    f = tmp_path / name
    f.write_bytes(text.encode("utf-8"))
    return f, text


async def test_paging_through_a_file_reaches_the_end(tmp_path):
    """**核心用例**：一段一段读，拼起来必须等于全文。

    直接对应实验里的失败形态：16848 字符的文件，模型调了 3 次 `read_file`
    （3 × 8000 = 24000，够覆盖全文），却只读到开头一半——因为每次都从第 0 个字符
    开始，三次返回同一段。**没有这条测试，"翻页"可能只是换了个说法重复第一页。**
    """
    f, text = _paged_file(tmp_path)

    collected = []
    offset = 0
    while True:
        out = await TOOLS["read_file"].fn(path=str(f), offset=offset)
        chunk = out.split("\n", 1)[1]          # 去掉开头的页标记
        if not chunk:
            break
        collected.append(chunk)
        offset += len(chunk)
        if offset >= len(text):
            break

    assert len(collected) == 3                  # 16848 / 8000 → 3 页
    assert "".join(collected) == text
    assert offset == len(text)


async def test_offset_is_counted_in_characters_not_bytes(tmp_path):
    """offset 按**字符**。按字节实现会在这里错位（中文每字 3 字节、emoji 4 字节）。

    模型脑子里数的是字符（它的原话是"没有 offset 参数"），字节语义会让它算不准，
    还会把 v1.0.1 刚清掉的那类"切在多字节字符中间"的 bug 请回来。
    """
    text = "中" * 100 + "😀" * 20 + "abc" * 50
    f = tmp_path / "mixed.txt"
    f.write_text(text, encoding="utf-8")

    for offset in (0, 1, 99, 100, 101, 120, 140):
        out = await TOOLS["read_file"].fn(path=str(f), offset=offset, limit=7)
        assert out.split("\n", 1)[1] == text[offset:offset + 7], f"offset={offset}"


async def test_limit_cannot_exceed_the_hard_result_ceiling(tmp_path):
    """`limit` 可以往小调、**不能往大调**——给游标，不给宽度。

    顶开这个出口等于把上下文窗口的控制权交给模型，那正是 v1.0.1 删掉 `max_bytes`
    的同一个错误。整条结果（标记 + 换行 + 正文）仍必须落在 `MAX_RESULT_CHARS` 内，
    否则 `Agent` 那层截断会啃掉正文，而页标记说的"到第 N 字符"就成了假话。
    """
    f, text = _paged_file(tmp_path)

    out = await TOOLS["read_file"].fn(path=str(f), limit=999999)

    assert len(out) <= MAX_RESULT_CHARS
    chunk = out.split("\n", 1)[1]
    assert text.startswith(chunk)
    assert len(chunk) > MAX_RESULT_CHARS - 100     # 标记只吃掉几十个字符，不是一大块


async def test_the_page_marker_does_not_eat_the_content(tmp_path):
    """页标记占额度这件事必须真的算进去，而且算准。

    标记是"第 x-y 字符"——如果正文被 `truncate()` 砍掉一截，这个区间就成了假的：
    模型据此算下一个 offset 会**跳字**或**重复**。
    """
    f, text = _paged_file(tmp_path)

    out = await TOOLS["read_file"].fn(path=str(f), limit=MAX_RESULT_CHARS)
    marker, chunk = out.split("\n", 1)
    end = int(marker.split("-")[1].split(" ")[0])

    assert len(chunk) == end                      # 标记说到哪，正文就真到哪
    assert chunk == text[:end]


async def test_a_page_continues_exactly_where_the_last_one_stopped(tmp_path):
    """两页之间**不重不漏**：第二个 offset 取上一页的结尾，接起来等于原文。

    这条是"翻页"与"重复读同一段"的分界线。标记若多报或少报一个字符，
    这里就会露出来——而模型正是拿标记里的数字去算下一个 offset 的。
    """
    f, text = _paged_file(tmp_path)

    first = await TOOLS["read_file"].fn(path=str(f))
    first_chunk = first.split("\n", 1)[1]
    second = await TOOLS["read_file"].fn(path=str(f), offset=len(first_chunk))
    second_chunk = second.split("\n", 1)[1]

    assert first_chunk + second_chunk == text[:len(first_chunk) + len(second_chunk)]
    assert len(second_chunk) > 0
    assert second_chunk != first_chunk


async def test_a_small_limit_is_honoured(tmp_path):
    """往小调是真实需求（"我只想要这一节的开头"），而且**要多少给多少**。

    标记不该从模型要的额度里扣：`limit=5` 就得回 5 个字符。扣了的话，
    小 limit 会被标记吃光、返回空内容——比不给这个参数还糟。
    """
    text = "abcdefghij" * 10
    f = tmp_path / "small.txt"
    f.write_text(text, encoding="utf-8")

    out = await TOOLS["read_file"].fn(path=str(f), offset=3, limit=5)

    assert out.split("\n", 1)[1] == "defgh"


async def test_page_marker_says_where_you_are_and_whether_more_follows(tmp_path):
    """页标记要给模型两件事：这是第几到第几个字符、后面还有没有。

    有了它，模型不必再烧一次调用去试探"到底有没有下一段"——它上次烧步数就是这个形态。
    这里只断言**这两件事**，不写死"第一页正好 8000 字"：正文长度随标记长度浮动，
    写死它等于把测试绑在一个无关的实现细节上。
    """
    f, text = _paged_file(tmp_path)

    first = await TOOLS["read_file"].fn(path=str(f))
    marker = first.split("\n", 1)[0]
    assert marker.startswith("…[第 1-")
    assert f"共 {len(text)} 字符" in marker
    assert "后面还有" in marker

    last = await TOOLS["read_file"].fn(path=str(f), offset=16000)
    assert last.startswith("…[第 16001-16848 字符，共 16848 字符")
    assert "后面还有" not in last.split("\n", 1)[0]


async def test_offset_past_the_end_says_so_instead_of_returning_nothing(tmp_path):
    """越界必须说清，不能返回空串。

    空串对模型是零信息（"读到空的"和"读不到"分不清），它多半会再试一次
    ——又是一次白烧的调用。所以这里还报出文件总长度，让它知道该退到哪。
    """
    f, text = _paged_file(tmp_path)

    at_end = await TOOLS["read_file"].fn(path=str(f), offset=len(text))
    past_end = await TOOLS["read_file"].fn(path=str(f), offset=len(text) + 999)

    for out in (at_end, past_end):
        assert "超出文件长度" in out
        assert str(len(text)) in out


@pytest.mark.parametrize("bad", [{"offset": -1}, {"limit": 0}, {"limit": -5}])
async def test_invalid_range_params_are_rejected(tmp_path, bad):
    """负偏移、零或负的 limit 都是参数错误——在进工具之前就被 pydantic 拦下。"""
    from pydantic import ValidationError

    text = "abcdefghij" * 10
    f = tmp_path / "small.txt"
    f.write_text(text, encoding="utf-8")

    with pytest.raises(ValidationError):
        TOOLS["read_file"].params(path=str(f), **bad)


async def test_a_cut_in_the_middle_of_a_character_is_not_reported_as_binary(tmp_path):
    """中文每字 3 字节，按字节切经常正好切在一个字中间。

    这种残片**是我们自己切的**，必须丢掉残字返回完整前缀，而不是把整份中文
    误报成二进制文件。旧实现靠传入小 `max_bytes` 构造这个输入；参数删掉后
    直接用 offset 构造，覆盖不丢。
    """
    f = tmp_path / "cn.txt"
    f.write_text("中文测试" * 100, encoding="utf-8")

    out = await TOOLS["read_file"].fn(path=str(f), offset=2, limit=1)

    assert "无法按 UTF-8 解码" not in out
    assert out.split("\n", 1)[1] == "测"


@pytest.mark.parametrize(
    "cut,expected",
    [(4, "好"), (5, "好"), (6, "好"), (7, "好😀")],
)
def test_every_byte_cut_of_a_four_byte_character_decodes(cut, expected):
    """**每一个**可能的字节切点都要能解出来——这是中文文本不被误报成二进制的全部保证。

    固定退 1/2/3 字节在这里是够的（一个字符最多 4 字节，切点离边界最多 3 字节），
    但"够"要靠枚举证明，不能靠推理：这四条覆盖了 4 字节字符的每一个切点。
    （旧实现有 `truncated` 参数来区分"我们切坏的"与"文件本身坏"；整块读之后
    这个参数没意义了，回退无条件生效——见下一条测试。）
    """
    assert _decode_or_none("好😀".encode("utf-8")[:cut]) == expected


def test_a_complete_binary_file_is_still_reported_as_binary():
    """回退不是万能的：坏字节在**中间**时，退末尾救不回来，必须报二进制。

    （盲目退 1/2/3 字节会把 `abc\\xff\\xfe` 变成 `abc` 返回——把二进制当文本悄悄
    读出来。所以回退只在出错区间**顶到末尾**时才做：那才是"我们切出来的半个字符"。）
    """
    assert _decode_or_none(b"abc\xff\xfe") is None


def test_binary_corruption_in_the_middle_is_not_masked_by_the_backoff():
    """同一个坑的另一种形态：末尾合法、坏在中间。"""
    assert _decode_or_none("中文".encode("utf-8") + b"\xff\xfe" + "好".encode("utf-8")) is None


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
