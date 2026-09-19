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
      * 怎么翻页（offset 设成**已读的字符数**、别重复读同一段）
    """
    description = openai_schema(TOOLS["read_file"])["function"]["description"]

    assert "8000" in description
    assert "不要重复读同一段" in description
    assert "没有能把上限调大的参数" in description
    assert "已读的字符数" in description      # 与 system prompt 同一句话（见下一条）


def test_the_prompt_and_the_description_teach_the_same_paging():
    """**两处必须说同一件事，而且要能被执行层面验尸。**

    模型在两个不同时刻读到这两串字：描述每轮随 schema 下发，prompt 在对话最前面。
    说法不同就等着被误导——而且这种错**不会让任何既有测试变红**：只要两边都含
    "offset" 这个词，测试就绿，而模型可能拿着错的那个值永远重读同一页。

    真实翻车过一次：描述写"**上次读到的**字符数"（读起来是"上一页有多宽"，= 7965），
    prompt 写"**已读的**字符数"（累计，= 15934）。照前者算，第二页会被无限重读
    （实测 8 次调用、只覆盖 63724 字符中的同一段）。

    所以这条测试盯的是**那个词**，不是"提到了 offset"：
      * 认**累计**语义（已读的字符数），不认**页宽**语义（上次读到的/本次读到的字符数）
      * 也拒绝按字节的历史写法（那会让模型算错位置）
    """
    from norma.agent import DEFAULT_SYSTEM_PROMPT

    texts = {
        "system prompt": DEFAULT_SYSTEM_PROMPT,
        "工具描述": TOOLS["read_file"].description,
    }
    for where, text in texts.items():
        assert "已读的字符数" in text, f"{where} 没说清 offset 该填什么"
        assert "不要重复读同一段" in text, f"{where} 没说不许重复读"
        assert "上次读到的字符数" not in text, f"{where} 用了页宽语义（会无限重读第二页）"
        assert "本次读到的字符数" not in text, f"{where} 用了页宽语义（会无限重读第二页）"

    # 参数本身的说明（模型每轮同样读得到）必须与上面**逐字**一致：
    # 按字符、不按字节，而且教的那个数就是"已读的字符数"
    offset_field = TOOLS["read_file"].params.model_fields["offset"]
    assert "已读的字符数" in offset_field.description
    assert "字符" in offset_field.description
    assert "字节" not in offset_field.description


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
    （v1.0.1 的终局审查抓到过这个错的另一种形态：标记被截断层砍掉了。）
    """
    f = tmp_path / "huge.txt"
    f.write_text("x" * 70000, encoding="utf-8")

    out = await TOOLS["read_file"].fn(path=str(f))

    assert "70000 字节" in out          # 真实**字节**大小，不是读到的 64000
    assert "只读了开头" in out


async def test_a_file_exactly_at_the_cap_is_not_reported_as_cut(tmp_path):
    """恰好等于读取上限的文件**不算被切**——差一就会在这里说谎。

    `size > _READ_CAP_BYTES` 写成 `>=` 的话，一个正好 64000 字节的文件会被标成
    "文件共 64000 字节，只读了开头 64000 字节"：模型会以为后面还有，去追一个不存在的
    尾部（白烧一步），或者干脆放弃已经读全的内容。整块都读进来了，就没什么可标的。
    """
    from norma.tools import _READ_CAP_BYTES

    text = "x" * _READ_CAP_BYTES
    f = tmp_path / "exact.txt"
    f.write_bytes(text.encode("utf-8"))

    out = await TOOLS["read_file"].fn(path=str(f))

    assert "只读了开头" not in out
    assert out.startswith("…[第 1-")     # 一页没装下，所以页标记照旧


async def test_a_cut_landing_mid_character_is_not_reported_as_binary(tmp_path):
    """读取上限正好落在多字节字符中间时，那半个字符是**我们切出来的**，不是二进制。

    中文每字 3 字节，64000 不是 3 的倍数，所以"截在字符中间"是常态而非巧合。
    构造要是**真的超过上限**的文件（64001 字节），上限那一刀就落在汉字中间；
    文件正好 64000 字节时不算被切——那半个字是文件的性质，按二进制报才对
    （见 test_a_complete_file_whose_tail_is_invalid_is_reported_as_binary）。
    """
    from norma.tools import _READ_CAP_BYTES

    f = tmp_path / "cn.txt"
    f.write_bytes(("中" * 22000).encode("utf-8")[:_READ_CAP_BYTES + 1])

    out = await TOOLS["read_file"].fn(path=str(f))

    assert "无法按 UTF-8 解码" not in out
    assert out.startswith("…[第 1-")
    assert "只读了开头" in out            # 确实是被我们切的


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

    assert len(collected) <= 3                  # 16848 字符 / 一页约 8000 → 3 页够
    assert "".join(collected) == text
    assert offset == len(text)


@pytest.mark.parametrize("offset", [0, 1, 2, 3, 42, 100, 101, 700, 701])
async def test_the_returned_page_really_starts_at_the_requested_character(
        tmp_path, offset):
    """返回的那段必须**真的**是原文的第 offset+1 个字符起——不能只是自称是。

    为什么不能用 `text[offset:offset+n]` 来断言：那是拿实现跟它自己比。**标记与正文
    由同一个 offset 推出**，所以两者天然自洽——一个错位的实现会诚实地标着
    "第 2-8 字符"、也诚实地返回它以为的第 2-8 字符，两边都不说谎，而给的并不是
    文件的第 2 个字符。核对位置才能发现这种事，核对自洽不能。

    文件内容按位置造得**唯一**（`<0000>中` 这种），所以"这段内容出现在哪"只有一个答案；
    否则重复字符会让 `str.index` 命中第一个出现处，核对不出错位。
    """
    text = "".join(f"<{i:04d}>" + ("中" if i % 3 else "😀") for i in range(300))
    f = tmp_path / "positioned.txt"
    f.write_bytes(text.encode("utf-8"))

    out = await TOOLS["read_file"].fn(path=str(f), offset=offset, limit=7)
    marker, chunk = out.split("\n", 1)

    assert len(chunk) == 7
    assert text.index(chunk) == offset        # 位置对，不只是内容自洽
    assert f"第 {offset + 1}-{offset + 7} 字符" in marker


async def test_a_tiny_limit_still_returns_that_many_characters(tmp_path):
    """标记**不从模型要的 limit 里扣**：`limit=5` 就得回 5 个字符。

    从 limit 扣的话，小 limit 会被标记（几十个字符）吃光、返回空内容——比不给这个
    参数还糟。额度是从**总上限**扣的（见下一条），两者不能混。
    """
    f, _ = _paged_file(tmp_path)

    out = await TOOLS["read_file"].fn(path=str(f), limit=5)

    assert len(out.split("\n", 1)[1]) == 5


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
    # 文件本身到头、也不是超上限文件：不加任何尾注，模型该收尾了
    assert "读不到" not in last.split("\n", 1)[0]


async def test_the_last_readable_page_says_the_rest_cannot_be_read(tmp_path):
    """超上限文件的**最后一页**必须明说"后面读不到了"，不能只是沉默。

    沉默会被读成"你自己判断吧"：一个照 prompt 办的模型看到标记不带"后面还有"、
    于是拿终点当 offset 再试一次——而那一次注定是"偏移超出文件长度"。
    实测（120000 字节的文件）：第 3 页就是可读末尾，第 4 次调用白烧在报错上。
    标记其实**知道**后面读不到（它已经写着"只读了开头 64000 字节"），就该直说。
    """
    text = "字" * 40000                      # 120000 字节，远超 64000
    f = tmp_path / "big.txt"
    f.write_bytes(text.encode("utf-8"))

    offset, pages = 0, 0
    while pages < 20:
        out = await TOOLS["read_file"].fn(path=str(f), offset=offset)
        marker, chunk = out.split("\n", 1)
        offset += len(chunk)
        pages += 1
        if "后面还有" not in marker:
            break

    assert pages == 3
    assert "只读了开头" in marker            # 文件比读到的长
    assert "读不到了" in marker              # 而这就是可读的末尾——直说
    assert "后面还有" not in marker


async def test_a_capped_file_mid_page_says_more_follows_within_what_is_readable(tmp_path):
    """超上限文件的**中途**页：既要"后面还有"，也要带上文件真实大小。

    "后面还有"在这里指"可读部分还没完"——与末页那句"后面读不到了"合起来，
    模型不必自己猜标记的沉默是什么意思（它就是拿这个决定要不要再读一次的）。
    """
    text = "字" * 40000                      # 120000 字节
    f = tmp_path / "big.txt"
    f.write_bytes(text.encode("utf-8"))

    out = await TOOLS["read_file"].fn(path=str(f))
    marker = out.split("\n", 1)[0]

    assert "后面还有" in marker
    assert "只读了开头" in marker
    assert "120000 字节" in marker           # 文件真实大小，不是读到的 64000
    assert "读不到了" not in marker


def test_the_page_budget_reserves_the_longest_possible_marker():
    """页标记的长度随 total/size 的**位数**变长，额度必须按最长的那版留。

    实测边界：4 字节字符的文件（1.2 亿字节、total 9 位）与一亿字节的 ASCII 文件，
    标记最长 63 字符，结果恰好 8000 不超。造 100 MB 的测试文件不现实，所以这里盯
    **预算那一步**：它估的标记必须不短于实际会产出的那一版。

    漏了的话后果很隐蔽：越界的部分被 `result[:MAX_RESULT_CHARS]` 截掉，
    而页标记声明的"到第 N 字符"就成了假话——模型拿它算下一个 offset 会**跳字**。
    """
    from norma.tools import _page_marker, _size_note

    big_size = _size_note(10**12)               # 13 位字节数：真实世界里到不了这么长
    # 预算那一步估的是"还有下文"的最长版（shown == total 时带不上尾注，故不短于实际）
    longest = _page_marker(offset=0, shown=10**9, total=10**9, size_note=big_size)
    actual = _page_marker(offset=0, shown=7936, total=16000, size_note=big_size)

    assert len(actual) <= len(longest)
    # 正文额度＝上限 − 估的标记 − 换行，仍然是个像样的正文长度（标记没有膨胀到吃掉正文）
    assert MAX_RESULT_CHARS - len(longest) - 1 > MAX_RESULT_CHARS - 100


async def test_a_full_page_uses_the_ceiling_without_shortchanging_the_body(tmp_path):
    """一页要把额度用足、且**声明多少就给多少**。

    判据不能是"没超上限"——截断能保证它。真正的坏法是预算估短了、正文多给几个字符、
    然后被 `result[:MAX_RESULT_CHARS]` 悄悄截掉尾巴：**没有任何报错，只是页比它自己
    声明的短**，而模型拿这个 N 算下一个 offset 就会漏读字符。

    留一点余量是对的（估算按最长的那版标记算，宁可少给几个字符也不能声明过头），
    所以这里只要求"用掉绝大部分额度"，不要求正好 8000。
    """
    text = "字" * 20000                       # 60000 字节，未到 64 KiB 墙
    f = tmp_path / "novel.txt"
    f.write_bytes(text.encode("utf-8"))

    out = await TOOLS["read_file"].fn(path=str(f), limit=MAX_RESULT_CHARS)
    marker, chunk = out.split("\n", 1)
    declared = int(marker.split("-")[1].split(" ")[0])

    assert len(out) <= MAX_RESULT_CHARS       # 不许超
    assert len(out) > MAX_RESULT_CHARS - 50   # 也不许浪费一大截额度
    assert len(chunk) == declared             # 声明到哪就真到哪
    assert chunk == text[:declared]
    assert "后面还有" in marker                # 这一页确实不是最后一页


@pytest.mark.parametrize("offset", list(range(2025, 2045)) + [2060, 2061, 2062, 2063, 2064, 2065])
async def test_a_page_ending_on_a_decimal_carry_still_fits(tmp_path, offset):
    """**回归测试**：正文变长可能让标记**多一位数**（9999 → 10000），额度得算进去。

    实测过的那一档（`offset=2034`、12035 字符的纯文本、`limit=8000`）：第二趟按第一趟
    的标记长度把正文补满，补完终点从 9999 变成 10000——**标记自己长了一位**，总长 8001，
    于是断言把一次正常的读取炸成"工具报错"（模型拿到的是内部诊断，不是那一页）。
    `python -O` 下断言被剥掉时更糟：截断削掉一个字符，页**声明的比给的多**，
    模型照它算下一个 offset 就漏读——正是这个版本要消灭的那类毛病。

    扫一串贴着进位点的 offset，因为坏窗口只有一个位置宽，单点测试守不住。
    """
    from norma.tools import _READ_CAP_BYTES

    for label, text in (("纯文本", "a" * 12035),
                        ("汉字", "字" * 12035),
                        ("超上限", "a" * (_READ_CAP_BYTES + 30000))):
        f = tmp_path / f"carry-{label}.txt"
        f.write_bytes(text.encode("utf-8"))

        out = await TOOLS["read_file"].fn(path=str(f), offset=offset)
        assert "工具报错" not in out, f"{label} offset={offset}"
        marker, chunk = out.split("\n", 1)
        lo, hi = (int(x) for x in marker.split("第 ")[1].split(" 字符")[0].split("-"))

        assert len(out) <= MAX_RESULT_CHARS, f"{label} offset={offset}: {len(out)}"
        assert len(chunk) == hi - lo + 1, f"{label} offset={offset} 标记说谎"
        assert chunk == text[lo - 1:hi], f"{label} offset={offset} 内容不符"


@pytest.mark.parametrize("limit", [7968, 7969, 7970, 7971, 7972, 7973, 7990, 8000])
async def test_no_limit_near_the_ceiling_makes_the_marker_lie(tmp_path, limit):
    """**回归测试**：`limit` 贴着上限时，标记绝不许声明得比正文多。

    实测过的那一档（`limit=7970, total=10000`）：预算按 `shown=total` 那版标记估，
    而那一版**带不上尾注**（少 5 个字符），于是正文多给几个字符、总数超 8000、
    收尾截断把尾巴削掉——标记写"第 1-7970"、实际只给了 7969。
    模型照着标记往后读，第 7970 个字符就永远读不到了。

    `limit` 取一串贴着上限的值，因为坏窗口只有几个字符宽，单点容易漏。
    """
    text = "n" * 10000
    f = tmp_path / "lies.txt"
    f.write_bytes(text.encode("utf-8"))

    out = await TOOLS["read_file"].fn(path=str(f), limit=limit)
    marker, chunk = out.split("\n", 1)
    declared = int(marker.split("-")[1].split(" ")[0])

    assert len(chunk) == declared, f"limit={limit}：标记声明 {declared}，实际 {len(chunk)}"
    assert chunk == text[:declared]
    assert len(out) <= MAX_RESULT_CHARS


async def test_paging_with_a_near_ceiling_limit_loses_no_characters(tmp_path):
    """贴着上限翻页也不许漏字——把"声明即下一个 offset"这条链走完。

    一条一条读下去，每一页都按标记声明的终点接上，最后拼起来必须**等于原文**。
    这是终局影响：漏一个字符在小说场景里就是少读一句，而循环不会报任何错。
    """
    text = "".join(f"<{i:05d}>" for i in range(1500))     # 10000 字符
    f = tmp_path / "paged.txt"
    f.write_bytes(text.encode("utf-8"))

    got, offset, pages = "", 0, 0
    while pages < 20:
        out = await TOOLS["read_file"].fn(path=str(f), offset=offset, limit=7972)
        if "超出文件长度" in out:
            break
        marker, chunk = out.split("\n", 1)
        declared_end = int(marker.split("-")[1].split(" ")[0])
        assert len(chunk) == declared_end - offset, "标记声明与正文长度不符"
        got += chunk
        offset = declared_end
        pages += 1

    assert got == text
    assert offset == len(text)

    # 回到真实尺度再验一遍真产物：70000 字节的文件必须正好卡在上限内
    # （见 test_limit_cannot_exceed_the_hard_result_ceiling，那条跑的是真的读文件）


async def test_an_empty_file_is_reported_as_empty_not_as_a_bad_offset(tmp_path):
    """空文件是**文件的性质**，不是模型填错了 offset。

    说成"偏移 0 超出文件长度"会把它引到错误的方向：去改那个本来就是 0 的 offset，
    而不是得出"这个文件没内容"的结论——一次白烧的调用。
    """
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")

    out = await TOOLS["read_file"].fn(path=str(f))

    assert "空" in out
    assert "超出文件长度" not in out
    assert "0 字节" in out


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


async def test_past_the_end_of_a_capped_file_also_reports_the_cap(tmp_path):
    """超过读取上限的文件，翻到最后一页之后也必须说清"后面读不到了"。

    否则模型看到的是一句干巴巴的"共 24000 字符"，会以为**文件就这么长**——
    页标记里那句"只读了开头"它已经看不到了（这是它对这个文件的最后一次读）。
    实测：72000 字符的文件翻到第 4 页之后正是这个形态。
    """
    from norma.tools import _READ_CAP_BYTES

    text = "x" * 72000
    f = tmp_path / "huge.txt"
    f.write_bytes(text.encode("utf-8"))

    # 一路翻到底
    offset, pages = 0, 0
    while pages < 20:
        out = await TOOLS["read_file"].fn(path=str(f), offset=offset)
        if "超出文件长度" in out:
            break
        offset += len(out.split("\n", 1)[1])
        pages += 1

    assert "超出文件长度" in out
    assert str(len(text)) in out                 # 文件真实长度
    assert "只读了开头" in out                    # 以及"读不到后面"这件事
    assert str(_READ_CAP_BYTES) in out


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
    "at,expected",
    [(4, "好"), (5, "好"), (6, "好"), (7, "好😀")],
)
def test_every_byte_cut_of_a_four_byte_character_decodes(at, expected):
    """**每一个**可能的字节切点都要能解出来——这是中文文本不被误报成二进制的全部保证。

    固定退 1/2/3 字节在这里是够的（一个字符最多 4 字节，切点离边界最多 3 字节），
    但"够"要靠枚举证明，不能靠推理：这四条覆盖了 4 字节字符的每一个切点。

    `cut=True` 是调用方给的信号（它知道文件超过读取上限）。**这个信号不能省**——
    见下一条。
    """
    assert _decode_or_none("好😀".encode("utf-8")[:at], cut=True) == expected


def test_a_complete_file_whose_tail_is_invalid_is_reported_as_binary():
    """**回归测试**：完整文件（没被我们切过）末尾有坏字节 → 它是二进制，不是文本。

    实测过的一次真回归：把"是不是我们切的"这个判断去掉、无条件退 1/2/3 字节之后，
    下面这些都成了"文本"（尾巴被悄悄丢掉）：

        b"abc\\xff"      → "abc"
        b"MZ\\x00\\x00\\xff" → "MZ\\x00\\x00"
        "café".encode("latin-1") → "caf"

    v1.0.1 把四个都判成二进制，而**二进制文件的尾巴常常正好是坏的**——
    光看"末尾有坏字节"分不出是谁造成的，只有调用方知道文件有没有被切过。
    """
    for data in (b"abc\xff", b"MZ\x00\x00\xff", b"PK\x03\x04" + b"\x00" * 20 + b"\xff",
                 "café".encode("latin-1")):
        assert _decode_or_none(data, cut=False) is None, data


def test_the_same_bytes_decode_when_they_were_cut_by_the_read_cap():
    """同样末尾有坏字节，但**是我们切的**（cut=True）→ 该救回来。

    两条合起来才是完整的判断：坏字节在末尾时，"是谁造成的"决定它是二进制
    还是被我们切断的中文。
    """
    assert _decode_or_none("中文".encode("utf-8")[:-1], cut=True) == "中文".encode(
        "utf-8")[:-1].decode("utf-8", "ignore") + ""      # 退掉半个字 → 只剩"中"
    assert _decode_or_none("中文".encode("utf-8"), cut=True) == "中文"


def test_binary_corruption_in_the_middle_is_not_masked_by_the_backoff():
    """坏在中间、末字节合法：退末尾救不回来，必须报二进制。"""
    assert _decode_or_none(
        "中文".encode("utf-8") + b"\xff\xfe" + "好".encode("utf-8"), cut=True) is None


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
