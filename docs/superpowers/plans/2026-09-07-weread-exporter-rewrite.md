# weread-exporter v4 重写实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重写为单文件 `export.py`:章节按纯标题命名、支持 `--chapters`/交互式选章、自适应等待提速至 0.3–0.6 秒/页,保留图文交错、断点续传、图片并发下载、合并全本。

**Architecture:** 保留旧版已验证的捕获内核(Canvas Hook、双页拆分、y 坐标图文交错、段落合并,从 `export_precise.py` 原样移植),围绕它重写流程层:目录全量扫描(增量滚动收集)→ 章节选择 → 目录点击定点跳转 → 按选中段翻页导出 → 标题命名落盘 → 续传/下载/合并。

**Tech Stack:** Python 3 + playwright(异步 API)+ pytest(纯函数测试)。无其他依赖。

**Spec:** `docs/superpowers/specs/2026-09-07-weread-exporter-refactor-design.md`(计划与规格冲突时以本节的"规格修正"为准)

## Global Constraints

- 单文件 `export.py`,取代 `export_precise.py` 与 `download_images.py`(最终任务删除旧文件)
- 所有文件写盘显式 `encoding="utf-8"`,临时文件 `.tmp` + `os.replace` 原子替换
- 捕获内核函数(`split_spread`/`chars_to_lines`/`build_page_blocks`/`render_chapter_md`/`img_filename`/`CANVAS_HOOK`/`VIEWPORT_IMGS_JS`/`CANVAS_RECTS_JS`)从 `export_precise.py` 移植,**不改行为**
- 章节编号一律 1-based,与目录展示编号一致;`catalog_idx` 贯穿 raw json/图片文件名/合并排序
- 测试命令统一用 `python -m pytest`(Windows Git Bash 环境,避免 PATH 问题)
- 浏览器自动化部分无法单元测试,靠任务内冒烟步骤验证(本机 `cache/browser_profile` 已有登录态,可直接跑)
- 冒烟用书:`e3d32fb0593388e3dde8006kda432420278da4fb5c6e9ad`(output 下已有其旧版导出,冒烟前先把旧目录改名备份)

## 规格修正(锁定实现口径,覆盖 spec 对应细节)

1. `_catalog.json` 存**纯标题数组** `["扉页", ...]`(与旧版格式兼容,idx 即位置+1),不是 spec 写的 `[{idx,title}]`
2. 合并文件写到 `output/<book_id>/书名.md`(**书的目录内**,`images/` 相对路径直接可用;旧版写到 `output/` 根下图片链接是断的,属修复)
3. 断点续传的"哪些章节未完成"用 raw json 的 `finished` 布尔标记判定:正常章节边界落盘 `finished:true`,异常中断的抢救性落盘 `finished:false`。未完成章(无 raw 或 `finished:false`)删除重导整章,修复旧版续传丢章头的缺陷。已完整重跑时不重导任何章
4. raw json 结构:`{"title", "catalog_idx", "file", "images", "text_len", "finished"}`,文件名 `{catalog_idx:04d}.json`。旧版无 `catalog_idx` 的 raw 一律忽略(视为无进度),旧数字命名 md 保留为遗留文件不清理

## File Structure

- Create: `export.py` — 全部实现(纯函数层 → 捕获内核 → 目录浏览器层 → 会话导出层 → 收尾层,按任务逐层追加)
- Create: `tests/test_pure.py` — 纯函数单元测试(随任务追加)
- Modify: `requirements.txt` — 加 pytest
- Modify: `README.md` — 重写用法(Task 6)
- Delete: `export_precise.py`、`download_images.py`(Task 6)

---

### Task 1: 纯函数层 — 选章解析/文件名净化/目录标题清理/标题匹配

**Files:**
- Create: `export.py`
- Create: `tests/test_pure.py`
- Modify: `requirements.txt`

**Interfaces:**
- Produces(后续任务依赖的确切签名):
  - `parse_chapter_spec(spec: str, total: int) -> set[int]` — 解析 `"5-12,18"` 为 1-based 编号集合;非法片段打印警告并跳过;空集=无效
  - `sanitize_filename(name: str, max_len: int = 80) -> str`
  - `dedup_filenames(titles: list) -> list[str]` — 全书目录→每章唯一文件名(不含 `.md`),重复标题追加 `" (2)"`
  - `clean_catalog_title(text: str) -> str` — 剥离目录条目混入的进度文字
  - `match_catalog_title(titles: list, header: str, from_pos: int)` — 顶部标题→目录编号(1-based)或 `None`;从 `from_pos+1` 起向后搜索,先去空白全等再互相包含

- [ ] **Step 1: 创建 `export.py` 骨架(文件头+导入)**

```python
#!/usr/bin/env python3
"""weread-exporter v4 — 微信读书导出:章节选择/标题命名/快速翻页。

在 v3(Canvas Hook 图文捕获)基础上重写流程层:
目录全量扫描 → 选章(--chapters 或交互) → 目录点击跳转分段导出
→ 章节按标题命名落盘 → 断点续传 → 并发下图 → 合并全本。
"""
import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright

USER_DATA_DIR = os.path.join("cache", "browser_profile")
```

- [ ] **Step 2: 写失败测试 `tests/test_pure.py`**

```python
# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from export import (parse_chapter_spec, sanitize_filename, dedup_filenames,
                    clean_catalog_title, match_catalog_title)


class TestParseChapterSpec:
    def test_range_and_single(self):
        assert parse_chapter_spec("5-12,18", 20) == set(range(5, 13)) | {18}

    def test_single(self):
        assert parse_chapter_spec("3", 10) == {3}

    def test_spaces_and_reversed_range(self):
        assert parse_chapter_spec(" 7 , 9-8 ", 10) == {7, 8, 9}

    def test_out_of_range_clamped(self):
        assert parse_chapter_spec("5-30", 20) == set(range(5, 21))

    def test_invalid_tokens_skipped(self):
        assert parse_chapter_spec("abc,5", 10) == {5}

    def test_zero_and_overflow_dropped(self):
        assert parse_chapter_spec("0,25", 20) == set()

    def test_empty_is_empty(self):
        assert parse_chapter_spec("", 20) == set()


class TestSanitizeFilename:
    def test_illegal_chars_replaced(self):
        out = sanitize_filename('a<b>c:"d/e\\f|g?h*i')
        for ch in '<>:"/\\|?*':
            assert ch not in out

    def test_trailing_dot_space_stripped(self):
        assert sanitize_filename("end. ") == "end"

    def test_long_truncated(self):
        assert len(sanitize_filename("字" * 100)) == 80

    def test_empty_becomes_underscore(self):
        assert sanitize_filename("   ") == "_"


class TestDedupFilenames:
    def test_duplicates_numbered(self):
        assert dedup_filenames(["注释", "扉页", "注释"]) == ["注释", "扉页", "注释 (2)"]

    def test_sanitizes_inside(self):
        assert dedup_filenames(["a/b", "a?b"]) == ["a_b", "a_b (2)"]


class TestCleanCatalogTitle:
    def test_progress_stripped(self):
        assert clean_catalog_title("版权信息当前读到 60%+书签") == "版权信息"

    def test_progress_only(self):
        assert clean_catalog_title("y 当前读到 12%") == "y"

    def test_bookmark_only(self):
        assert clean_catalog_title("z+书签") == "z"

    def test_clean_untouched(self):
        assert clean_catalog_title("第1章 阅读前的准备工作") == "第1章 阅读前的准备工作"


class TestMatchCatalogTitle:
    TITLES = ["扉页", "推荐序", "注释", "第1章 开始", "注释"]

    def test_exact_forward(self):
        assert match_catalog_title(self.TITLES, "第1章 开始", 0) == 4

    def test_duplicate_skips_earlier(self):
        assert match_catalog_title(self.TITLES, "注释", 2) == 5

    def test_containment_fallback(self):
        assert match_catalog_title(["推荐序一"], "推荐序", 0) == 1

    def test_whitespace_normalized(self):
        assert match_catalog_title([" 第1章 开始 "], "第1章 开始", 0) == 1

    def test_not_found(self):
        assert match_catalog_title(self.TITLES, "不存在的章", 0) is None

    def test_empty_header(self):
        assert match_catalog_title(self.TITLES, "", 0) is None
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/test_pure.py -v`
Expected: FAIL,`ImportError: cannot import name 'parse_chapter_spec'`

- [ ] **Step 4: 在 `export.py` 实现五个纯函数**

```python
# ---------- 纯函数:选章解析 / 文件名 ----------

def parse_chapter_spec(spec, total):
    """解析 "5-12,18" → {5..12, 18}(1-based)。非法片段警告跳过,越界截断/丢弃。"""
    out = set()
    for part in spec.split(","):
        p = part.strip()
        if not p:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", p)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            for i in range(a, b + 1):
                if i > total:
                    break
                if i >= 1:
                    out.add(i)
        elif p.isdigit():
            i = int(p)
            if 1 <= i <= total:
                out.add(i)
        else:
            print(f"  ⚠️  忽略无法解析的章节片段: 「{p}」")
    return out


def sanitize_filename(name, max_len=80):
    """Windows 安全文件名:非法字符→_,去首尾空白与结尾点号,截断。"""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    s = s.rstrip(". ")
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    return s or "_"


def dedup_filenames(titles):
    """全书目录 → 每章唯一文件名(不含扩展名);重复标题追加 (2)(3)...(按目录序)。"""
    seen = {}
    out = []
    for t in titles:
        base = sanitize_filename(t)
        n = seen.get(base, 0) + 1
        seen[base] = n
        out.append(base if n == 1 else f"{base} ({n})")
    return out


def clean_catalog_title(text):
    """剥离目录条目混入的进度文字(如「版权信息当前读到 60%+书签」→「版权信息」)。"""
    s = re.sub(r"当前读到.*$", "", text)
    s = re.sub(r"\+?\s*书签\s*$", "", s)
    return s.strip()


def match_catalog_title(titles, header, from_pos):
    """顶部标题 → 目录编号(1-based);从 from_pos+1 向后搜索。
    先去空白全等,再互相包含(兜住目录标题剥离不净的情况)。找不到返回 None。"""
    def norm(s):
        return re.sub(r"\s+", "", s or "")

    h = norm(header)
    if not h:
        return None
    order = range(from_pos + 1, len(titles) + 1)
    for idx in order:
        if norm(titles[idx - 1]) == h:
            return idx
    for idx in order:
        t = norm(titles[idx - 1])
        if t and (t in h or h in t):
            return idx
    return None
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_pure.py -v`
Expected: 全部 PASS

- [ ] **Step 6: requirements.txt 加 pytest 并提交**

`requirements.txt` 全文:

```
playwright>=1.40
pytest
```

```bash
git add export.py tests/test_pure.py requirements.txt
git commit -m "feat: v4 纯函数层(选章解析/文件名净化/目录标题清理/标题匹配)"
```

---

### Task 2: 捕获内核移植(原样,不改行为)

**Files:**
- Modify: `export.py`(追加)
- Modify: `tests/test_pure.py`(追加测试)

**Interfaces:**
- Consumes: Task 1 的文件骨架
- Produces(签名不变,来源 `export_precise.py` 行号):
  - `split_spread(chars)` — L88-98
  - `chars_to_lines(chars)` — L101-115
  - `build_page_blocks(chars, images, canvas_rects, seen_imgs)` — L118-158
  - `img_filename(url, ch_idx, seq)` — L161-166(ch_idx 即 catalog_idx)
  - `render_chapter_md(ch_title, blocks, ch_idx)` — L169-206,返回 `(body, img_records)`
  - 常量 `MEASURE_RE`/`SENTENCE_END`(L84-85)、`CANVAS_HOOK`(L42-54)、`VIEWPORT_IMGS_JS`(L57-74)、`CANVAS_RECTS_JS`(L77-82)

- [ ] **Step 1: 追加失败测试(行为快照)**

追加到 `tests/test_pure.py`:

```python
from export import (split_spread, chars_to_lines, render_chapter_md,
                    MEASURE_RE, SENTENCE_END)  # noqa: E402


def _ch(t, x, y):
    return {"t": t, "x": x, "y": y}


class TestSplitSpread:
    def test_two_pages_by_y_reset(self):
        chars = ([_ch(chr(65 + i), 10, 120 + i * 30) for i in range(11)] +
                 [_ch(chr(97 + i), 10, 130 + i * 30) for i in range(11)])
        pages = split_spread(chars)
        assert len(pages) == 2
        assert pages[0][0]["t"] == "A"
        assert pages[1][0]["t"] == "a"

    def test_short_stays_single(self):
        assert len(split_spread([_ch("a", 1, 10)])) == 1


class TestCharsToLines:
    def test_rows_grouped_by_y_and_sorted_by_x(self):
        chars = [_ch("界", 20, 100), _ch("世", 10, 101), _ch("好", 10, 200), _ch("人", 20, 199)]
        lines = chars_to_lines(chars)
        assert [l["text"] for l in lines] == ["世界", "好人"]

    def test_measure_only_dropped(self):
        assert chars_to_lines([_ch(" .,1", 1, 10)]) == []


class TestRenderChapterMd:
    def test_lines_merged_into_paragraphs(self):
        blocks = [{"type": "text", "text": "这是第一行没有标点"},
                  {"type": "text", "text": "接续第二行。"},
                  {"type": "text", "text": "新段落。"}]
        body, imgs = render_chapter_md("测试章", blocks, 7)
        assert "# 测试章" in body
        assert "这是第一行没有标点接续第二行。" in body
        assert "新段落。" in body
        assert imgs == []

    def test_image_breaks_paragraph(self):
        blocks = [{"type": "text", "text": "段落一。"},
                  {"type": "img", "src": "http://x/a.jpg", "w": 1, "h": 1},
                  {"type": "text", "text": "段落二。"}]
        body, imgs = render_chapter_md("t", blocks, 3)
        assert "![图](images/ch0003_img01.jpg)" in body
        assert imgs == [{"url": "http://x/a.jpg", "file": "ch0003_img01.jpg"}]

    def test_title_line_not_repeated_in_body(self):
        blocks = [{"type": "text", "text": "章节标题"},
                  {"type": "text", "text": "正文。"}]
        body, _ = render_chapter_md("章节标题", blocks, 1)
        assert body.count("章节标题") == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_pure.py -v`
Expected: 新增用例 FAIL(`ImportError: cannot import name 'split_spread'`)

- [ ] **Step 3: 从 `export_precise.py` 原样移植**

把 `export_precise.py` 的以下片段复制进 `export.py`(放在纯函数之后),**逐字不变**:
常量 `MEASURE_RE`、`SENTENCE_END`(L84-85);`CANVAS_HOOK`(L42-54)、`VIEWPORT_IMGS_JS`(L57-74)、`CANVAS_RECTS_JS`(L77-82)放在常量区;函数 `split_spread`(L88-98)、`chars_to_lines`(L101-115)、`build_page_blocks`(L118-158)、`img_filename`(L161-166)、`render_chapter_md`(L169-206)。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_pure.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add export.py tests/test_pure.py
git commit -m "feat: 移植 v3 捕获内核(双页拆分/图文交错/段落合并,行为不变)"
```

---

### Task 3: 目录浏览器层 — 全量扫描 + 定点跳转 + `--scan-only`

**Files:**
- Modify: `export.py`(追加)
- Modify: `tests/test_pure.py`(追加)

**Interfaces:**
- Consumes: Task 1 `clean_catalog_title`;Task 2 `CANVAS_HOOK`
- Produces:
  - `locate_slice(titles: list, visible: list)` → 渲染切片在全目录中的起始下标(0-based)或 `None`
  - `async open_catalog_panel(page)` / `async close_catalog_panel(page)`
  - `async visible_titles(page) -> list[str]`(已清理进度的可见目录条目,DOM 序)
  - `async scan_full_catalog(page) -> list[str]`(全书标题,按目录序)
  - `async jump_to_chapter(page, target_idx, titles)`(target_idx 1-based;失败抛 `RuntimeError`)
  - `async ensure_login(ctx)`(未登录等扫码,10 分钟超时抛 `RuntimeError`)
  - `async fetch_book_title(page) -> (title, author)`
  - `asynccontextmanager reader_session(book_id)`(yield 已注入 Hook、已打开阅读器的 page)
  - `async scan_catalog_session(book_id, catalog_path) -> list[str]`
  - `parse_args(argv=None)` / `extract_book_id(raw) -> str`
  - JS 常量 `CATALOG_ITEMS_JS`、`CATALOG_SCROLL_JS`、`CATALOG_BOTTOM_JS`、`CATALOG_TOP_JS`

- [ ] **Step 1: 追加失败测试(locate_slice)**

```python
from export import locate_slice  # noqa: E402


class TestLocateSlice:
    def test_overlap_found(self):
        titles = ["a", "b", "c", "d", "e"]
        assert locate_slice(titles, ["c", "d"]) == 2

    def test_no_match(self):
        assert locate_slice(["a", "b"], ["x"]) is None

    def test_full_match_at_zero(self):
        assert locate_slice(["a", "b"], ["a", "b"]) == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_pure.py -v`
Expected: FAIL(`cannot import name 'locate_slice'`)

- [ ] **Step 3: 实现目录浏览器层**

追加到 `export.py`(`kill_stale_browsers`、登录/书名抓取从 `export_precise.py` 移植:前者 L23-39 原样,后两者按下面整理过的版本):

```python
# ---------- 目录浏览器层 ----------

# 目录条目(清理前的原始文本,DOM 序)
CATALOG_ITEMS_JS = """
() => Array.from(document.querySelectorAll('.readerCatalog_list_item')).map(el => {
    const t = el.querySelector('.readerCatalog_list_title');
    return ((t ? t.textContent : el.textContent) || '').trim();
}).filter(s => s)
"""

CATALOG_SCROLL_JS = """
(delta) => {
    const sc = document.querySelector('.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]');
    if (sc) sc.scrollTop += delta;
    return sc ? sc.scrollTop : -1;
}
"""

CATALOG_BOTTOM_JS = """
() => {
    const sc = document.querySelector('.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]');
    return !!sc && sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 4;
}
"""

CATALOG_TOP_JS = """
() => {
    const sc = document.querySelector('.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]');
    if (sc) sc.scrollTop = 0;
}
"""


def locate_slice(titles, visible):
    """当前渲染切片(虚拟列表只渲染可见窗口)在完整目录中的起始下标。
    返回最小的 o 使 titles[o:o+len(visible)] == visible;找不到返回 None。"""
    n = len(visible)
    if n == 0:
        return None
    for o in range(0, len(titles) - n + 1):
        if titles[o:o + n] == visible:
            return o
    return None


async def open_catalog_panel(page):
    await page.click("button.readerControls_item.catalog", timeout=5000)
    await asyncio.sleep(1.0)


async def close_catalog_panel(page):
    try:
        await page.click("button.readerControls_item.catalog", timeout=2000)
    except Exception:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
    await asyncio.sleep(0.8)


async def visible_titles(page):
    raw = await page.evaluate(CATALOG_ITEMS_JS)
    out = []
    for t in raw:
        c = clean_catalog_title(t)
        if c:
            out.append(c)
    return out


async def scan_full_catalog(page):
    """打开目录面板,小步滚动到底,按序收集全部章节标题(虚拟列表增量渲染)。"""
    await open_catalog_panel(page)
    await page.evaluate(CATALOG_TOP_JS)
    await asyncio.sleep(0.5)
    known = []
    stagnant = 0
    while stagnant < 12:
        vis = await visible_titles(page)
        appended = 0
        if vis:
            if not known:
                known = list(vis)
                appended = len(vis)
            else:
                start = locate_slice(known, vis)
                if start is not None:
                    tail = start + len(vis)
                    if tail > len(known):
                        appended = tail - len(known)
                        known.extend(vis[len(vis) - appended:])
        if appended == 0:
            stagnant += 1
            if await page.evaluate(CATALOG_BOTTOM_JS):
                break
        else:
            stagnant = 0
        await page.evaluate(CATALOG_SCROLL_JS, 400)
        await asyncio.sleep(0.2)
    await close_catalog_panel(page)
    return known


async def jump_to_chapter(page, target_idx, titles):
    """打开目录,滚动到第 target_idx(1-based)章条目并点击跳转。失败抛 RuntimeError。"""
    await open_catalog_panel(page)
    target_title = titles[target_idx - 1]
    try:
        await page.evaluate(CATALOG_TOP_JS)
        await asyncio.sleep(0.4)
        for _ in range(800):
            vis = await visible_titles(page)
            if vis:
                start = locate_slice(titles, vis)
                if start is not None and start <= target_idx - 1 < start + len(vis):
                    k = target_idx - 1 - start
                    await page.locator(".readerCatalog_list_item").nth(k).click(timeout=4000)
                    return
            await page.evaluate(CATALOG_SCROLL_JS, 300)
            await asyncio.sleep(0.2)
        raise RuntimeError(f"目录中定位不到第 {target_idx} 章「{target_title}」")
    finally:
        await close_catalog_panel(page)
        await asyncio.sleep(2.5)


async def ensure_login(ctx):
    login_page = await ctx.new_page()
    await login_page.goto("https://weread.qq.com/web/shelf", timeout=30000)
    await asyncio.sleep(3)
    if "login" in login_page.url.lower():
        print("\n  ⚠️  请扫码登录微信读书")
        for _ in range(120):
            await asyncio.sleep(5)
            if "login" not in login_page.url.lower():
                print("  ✅ 登录成功")
                break
        else:
            await login_page.close()
            raise RuntimeError("登录超时(10 分钟)")
    else:
        print("  ✅ 已登录")
    await login_page.close()


async def fetch_book_title(page):
    info = await page.evaluate("""() => {
        const title = document.querySelector('.readerCatalog_bookInfo_title_txt, .bookInfo_right_header_title')
            ?.textContent?.trim() || document.title.replace(/-.*$/, '').trim();
        const author = document.querySelector('.readerCatalog_bookInfo_author, .bookInfo_author a')
            ?.textContent?.trim() || '';
        return {title, author};
    }""")
    return info.get("title", "未知"), info.get("author", "")


@asynccontextmanager
async def reader_session(book_id):
    """打开持久化浏览器 → 确保登录 → 打开阅读器并注入 Canvas Hook → yield page。"""
    async with async_playwright() as p:
        kill_stale_browsers()
        ctx = await p.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=False, viewport={"width": 1200, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--disable-gpu"])
        try:
            await ensure_login(ctx)
            page = await ctx.new_page()
            await page.add_init_script(CANVAS_HOOK)
            await page.goto(f"https://weread.qq.com/web/reader/{book_id}",
                            wait_until="networkidle", timeout=30000)
            await asyncio.sleep(4)
            yield page
            await page.close()
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


async def scan_catalog_session(book_id, catalog_path):
    async with reader_session(book_id) as page:
        titles = await scan_full_catalog(page)
    with open(catalog_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(titles, f, ensure_ascii=False)
    os.replace(catalog_path + ".tmp", catalog_path)
    return titles
```

同时追加 CLI 与入口(Task 5 会扩展 `main`):

```python
# ---------- CLI ----------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="weread-exporter v4 — 微信读书导出(选章/标题命名/快速)")
    ap.add_argument("book", help="reader URL 或 book_id")
    ap.add_argument("--chapters", default=None,
                    help='要导出的章节, 如 "5-12,18"(1-based;不传则交互选择/沿用上次)')
    ap.add_argument("--download-only", action="store_true",
                    help="只补下 raw 里记录的图片, 不打开浏览器")
    ap.add_argument("--scan-only", action="store_true",
                    help="只扫描完整目录存 _catalog.json, 不导出")
    return ap.parse_args(argv)


def extract_book_id(raw):
    raw = raw.strip().rstrip("/")
    return raw.split("/")[-1] if "weread.qq.com" in raw else raw


async def main(argv=None):
    args = parse_args(argv)
    book_id = extract_book_id(args.book)
    print("=" * 60)
    print("  weread-exporter v4 — 选章 / 标题命名 / 快速")
    print("=" * 60)
    print(f"  Book ID: {book_id}")
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    book_dir = os.path.join("output", book_id)
    catalog_path = os.path.join(book_dir, "_catalog.json")
    os.makedirs(book_dir, exist_ok=True)

    if args.scan_only:
        titles = await scan_catalog_session(book_id, catalog_path)
        print(f"  ✅ 目录共 {len(titles)} 章 → {catalog_path}")
        for i, t in enumerate(titles, 1):
            print(f"  [{i:4d}] {t}")
        return
    print("  (完整流程在 Task 5 装配)")


if __name__ == "__main__":
    asyncio.run(main())
```

`kill_stale_browsers` 从 `export_precise.py` L23-39 原样复制到目录浏览器层之前。

- [ ] **Step 4: 跑单元测试确认通过**

Run: `python -m pytest tests/test_pure.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 冒烟 — 真实扫描目录**

```bash
mv output/e3d32fb0593388e3dde8006kda432420278da4fb5c6e9ad output/e3d32fb0593388e3dde8006kda432420278da4fb5c6e9ad.bak
python export.py e3d32fb0593388e3dde8006kda432420278da4fb5c6e9ad --scan-only
```

Expected: 弹出浏览器自动复用登录态,打印完整章节清单(约 250+ 章,含"第1章 阅读前的准备工作"),`output/<book_id>/_catalog.json` 生成且为纯标题数组、顺序与微信读书目录一致。若 `.readerCatalog_list_title` 子元素选择器无效导致条目仍混进度文字,`clean_catalog_title` 应回退剥离;若面板滚动选择器失配,用浏览器 devtools 确认真实类名后只改三个 JS 常量的选择器。

- [ ] **Step 6: 提交**

```bash
git add export.py tests/test_pure.py
git commit -m "feat: 目录全量扫描/定点跳转/阅读器会话 + --scan-only"
```

---

### Task 4: 会话导出核心 — 段导出/自适应等待/落盘/续传

**Files:**
- Modify: `export.py`(追加)
- Modify: `tests/test_pure.py`(追加)

**Interfaces:**
- Consumes: Task 1 `match_catalog_title`/`dedup_filenames`;Task 2 `build_page_blocks`/`render_chapter_md`/JS 常量;Task 3 `reader_session`/`jump_to_chapter`/`fetch_book_title`
- Produces:
  - `async wait_render_stable(page, min_elapsed=0.25, poll=0.12, timeout=8.0) -> int`(稳定后的字符数)
  - `async header_title(page) -> str`
  - `compute_segments(indices: set) -> list[list[int]]`(升序连续段)
  - `remaining_work(selected: set, raw_dir) -> set[int]`(无 raw 或 raw `finished` 非 true 的选中章)
  - `save_chapter(ch_title, blocks, catalog_idx, md_dir, raw_dir, name_map, finished=True) -> (text_len, img_records)`
  - `purge_stale_chapters(raw_dir, md_dir, remaining, seen_imgs)`(删未完成章 raw+md,撤销其图片 url)
  - `load_seen_imgs(raw_dir) -> set` / `latest_done_idx(raw_dir) -> int`
  - `async export_segment(page, seg, titles, selected, md_dir, raw_dir, name_map, seen_imgs) -> (saved_count, reason)`
  - `async run_session(book_id, md_dir, raw_dir, catalog, selected, name_map, seen_imgs) -> dict`(键 `book_title`/`book_author`/`error`/`completed`)

- [ ] **Step 1: 追加失败测试**

```python
import json

from export import compute_segments, remaining_work  # noqa: E402


class TestComputeSegments:
    def test_splits_runs(self):
        assert compute_segments({5, 6, 7, 9, 12, 13}) == [[5, 6, 7], [9], [12, 13]]

    def test_empty(self):
        assert compute_segments(set()) == []


class TestRemainingWork:
    def test_no_raw_dir(self, tmp_path):
        assert remaining_work({1, 2, 3}, str(tmp_path / "none")) == {1, 2, 3}

    def test_finished_excluded_unfinished_kept(self, tmp_path):
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "0001.json").write_text(
            json.dumps({"catalog_idx": 1, "finished": True}), encoding="utf-8")
        (raw / "0002.json").write_text(
            json.dumps({"catalog_idx": 2, "finished": False}), encoding="utf-8")
        (raw / "old.json").write_text(
            json.dumps({"title": "旧版无 catalog_idx"}), encoding="utf-8")
        assert remaining_work({1, 2, 3, 4}, str(raw)) == {2, 3, 4}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_pure.py -v`
Expected: FAIL(`cannot import name 'compute_segments'`)

- [ ] **Step 3: 实现会话导出核心**

追加到 `export.py`:

```python
# ---------- 会话导出核心 ----------

async def header_title(page):
    return await page.evaluate(
        "() => document.querySelector('.renderTargetPageInfo_header_chapterTitle')"
        "?.textContent?.trim() || ''")


async def wait_render_stable(page, min_elapsed=0.25, poll=0.12, timeout=8.0):
    """翻页后自适应等待:轮询字符数,连续两次一致且距翻页不少于 min_elapsed 即返回。
    取代 v3 的固定 sleep(1.0)+0.5s 轮询,典型页耗时约 0.3-0.6s。"""
    t0 = time.monotonic()
    last = -1
    while True:
        c = await page.evaluate("() => window.__wr_count()")
        if c == last and (time.monotonic() - t0) >= min_elapsed:
            return c
        if time.monotonic() - t0 > timeout:
            return c
        last = c
        await asyncio.sleep(poll)


def compute_segments(indices):
    """选中编号 → 升序连续段列表,每段整段跳转一次。"""
    segs = []
    for x in sorted(indices):
        if segs and x == segs[-1][-1] + 1:
            segs[-1].append(x)
        else:
            segs.append([x])
    return segs


def remaining_work(selected, raw_dir):
    """选中章中未完成的:无 raw 记录,或 raw 的 finished 不为 true。"""
    finished = set()
    if os.path.isdir(raw_dir):
        for jf in os.listdir(raw_dir):
            if not jf.endswith(".json"):
                continue
            try:
                with open(os.path.join(raw_dir, jf), encoding="utf-8") as f:
                    r = json.load(f)
            except Exception:
                continue
            if r.get("finished") and isinstance(r.get("catalog_idx"), int):
                finished.add(r["catalog_idx"])
    return {i for i in selected if i not in finished}


def latest_done_idx(raw_dir):
    best = 0
    if os.path.isdir(raw_dir):
        for jf in os.listdir(raw_dir):
            if jf.endswith(".json"):
                try:
                    with open(os.path.join(raw_dir, jf), encoding="utf-8") as f:
                        ci = json.load(f).get("catalog_idx")
                except Exception:
                    continue
                if isinstance(ci, int):
                    best = max(best, ci)
    return best


def load_seen_imgs(raw_dir):
    seen = set()
    if os.path.isdir(raw_dir):
        for jf in os.listdir(raw_dir):
            if jf.endswith(".json"):
                try:
                    with open(os.path.join(raw_dir, jf), encoding="utf-8") as f:
                        r = json.load(f)
                except Exception:
                    continue
                for im in r.get("images", []):
                    seen.add(im.get("url"))
    return seen


def purge_stale_chapters(raw_dir, md_dir, remaining, seen_imgs):
    """删除未完成章节(raw+md)并从 seen_imgs 撤销其图片,重导时从头完整捕获。"""
    if not os.path.isdir(raw_dir):
        return
    for jf in list(os.listdir(raw_dir)):
        if not jf.endswith(".json"):
            continue
        path = os.path.join(raw_dir, jf)
        try:
            with open(path, encoding="utf-8") as f:
                r = json.load(f)
        except Exception:
            continue
        if r.get("catalog_idx") in remaining:
            md = os.path.join(md_dir, r.get("file", ""))
            for p in (md, path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            for im in r.get("images", []):
                seen_imgs.discard(im.get("url"))


def save_chapter(ch_title, blocks, catalog_idx, md_dir, raw_dir, name_map, finished=True):
    """按标题命名落盘一章:chapters/<标题>.md + raw/<idx>.json(原子写)。"""
    body, img_records = render_chapter_md(ch_title, blocks, catalog_idx)
    text_len = sum(len(b["text"]) for b in blocks if b["type"] == "text")
    if text_len == 0 and not img_records:
        return 0, []
    fname = name_map[catalog_idx - 1] + ".md"
    md_path = os.path.join(md_dir, fname)
    with open(md_path + ".tmp", "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(md_path + ".tmp", md_path)
    rec = {"title": ch_title, "catalog_idx": catalog_idx, "file": fname,
           "images": img_records, "text_len": text_len, "finished": finished}
    raw_path = os.path.join(raw_dir, f"{catalog_idx:04d}.json")
    with open(raw_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(raw_path + ".tmp", raw_path)
    return text_len, img_records


async def export_segment(page, seg, titles, selected, md_dir, raw_dir, name_map, seen_imgs):
    """导出一个连续章节段 seg(1-based 目录编号列表)。返回 (保存章数, 结束原因)。"""
    start, end = seg[0], seg[-1]
    print(f"  ▶ 段 {start}-{end}: 跳到「{titles[start - 1]}」")
    await jump_to_chapter(page, start, titles)

    cur_title = await header_title(page)
    m = match_catalog_title(titles, cur_title, start - 1)
    if m is None:
        print(f"  ⚠️  跳转后标题「{cur_title}」与目录第 {start} 章不符, 按该章继续")
        cur_pos = start
    else:
        cur_pos = m
    blocks = []
    warned = set()
    saved = [0]

    def save_and_clear(note="", finished=True):
        if not blocks:
            return
        n, imgs = save_chapter(cur_title, blocks, cur_pos, md_dir, raw_dir,
                               name_map, finished=finished)
        if n or imgs:
            saved[0] += 1
            img_note = f" +{len(imgs)}图" if imgs else ""
            print(f"  [{cur_pos:4d}] {cur_title[:36]:36s} {n:6d}字{img_note}{note}")
        blocks.clear()

    async def capture_once():
        await asyncio.sleep(0.15)
        chars = await page.evaluate("() => window.__wr_chars")
        rects = await page.evaluate(CANVAS_RECTS_JS)
        imgs = await page.evaluate(VIEWPORT_IMGS_JS)
        added = 0
        for b in build_page_blocks(chars, imgs, rects, seen_imgs):
            if (b["type"] == "text" and blocks
                    and blocks[-1].get("type") == "text"
                    and blocks[-1]["text"] == b["text"]):
                continue
            blocks.append(b)
            added += 1
        return added

    # 首页
    await page.evaluate("() => window.__wr_reset()")
    await wait_render_stable(page)
    await capture_once()

    stale = 0
    try:
        while True:
            await page.evaluate("() => window.__wr_reset()")
            await page.mouse.click(600, 450)
            await page.keyboard.press("ArrowRight")
            await wait_render_stable(page)

            new_title = await header_title(page)
            if new_title and new_title != cur_title:
                m = match_catalog_title(titles, new_title, cur_pos)
                if m is None:
                    if new_title not in warned:
                        print(f"  ⚠️  顶部标题「{new_title}」在目录中定位不到, 按同章继续")
                        warned.add(new_title)
                else:
                    save_and_clear()
                    if m > end or m not in selected:
                        return saved[0], "segment_end"
                    cur_pos, cur_title = m, new_title
                    await capture_once()  # 新章首页
                    stale = 0
                    continue

            added = await capture_once()
            if added == 0:
                stale += 1
                if stale >= 10:
                    save_and_clear(" [连续无新内容, 判定本段结束]")
                    return saved[0], "stale"
            else:
                stale = 0
    finally:
        # 异常中断:把已捕获的半章抢救落盘(finished=False, 续传时整章重导)
        if blocks:
            save_and_clear("  [中断保存]", finished=False)


async def run_session(book_id, md_dir, raw_dir, catalog, selected, name_map, seen_imgs):
    """一次浏览器会话:打开阅读器→清理未完成章→逐段导出。异常交外层重试。"""
    res = {"book_title": "", "book_author": "", "error": False, "completed": False}
    remaining = remaining_work(selected, raw_dir)
    if not remaining:
        res["completed"] = True
        return res
    purge_stale_chapters(raw_dir, md_dir, remaining, seen_imgs)
    try:
        async with reader_session(book_id) as page:
            res["book_title"], res["book_author"] = await fetch_book_title(page)
            await page.mouse.click(600, 450)
            await asyncio.sleep(0.5)
            segments = compute_segments(remaining)
            preview = ", ".join(
                f"{s[0]}-{s[-1]}" if len(s) > 1 else str(s[0]) for s in segments)
            print(f"  剩余 {len(remaining)} 章, 分 {len(segments)} 段: {preview}")
            for seg in segments:
                n, reason = await export_segment(
                    page, seg, catalog, selected, md_dir, raw_dir, name_map, seen_imgs)
                print(f"  ✔ 段 {seg[0]}-{seg[-1]}: {n} 章 ({reason})")
            res["completed"] = not remaining_work(selected, raw_dir)
    except Exception as e:
        res["error"] = True
        first = str(e).splitlines()[0] if str(e) else ""
        print(f"\n  ⚠️  会话异常中断: {type(e).__name__}: {first[:100]}")
    return res
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_pure.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add export.py tests/test_pure.py
git commit -m "feat: 会话导出核心(段跳转/自适应等待/标题落盘/finished 断点续传)"
```

---

### Task 5: 主流程装配 — 选择持久化/交互/图片下载/合并 + 端到端冒烟

**Files:**
- Modify: `export.py`(替换 Task 3 的临时 `main`,追加收尾层)
- Modify: `tests/test_pure.py`(无新增,回归)

**Interfaces:**
- Consumes: 前面全部任务
- Produces:
  - `load_catalog(path) -> list | None` / `save_selection(path, selected, book_title, book_author)` / `load_selection(path) -> dict | None`
  - `resolve_selection(spec, selection_path, catalog) -> set`(参数优先→沿用 `_selection.json`→交互)
  - `prompt_selection(catalog) -> set`
  - `download_all_images(raw_dir, img_dir, workers=8) -> int`(成功+跳过数;强制 IPv4 + 并发)
  - `merge_chapters(book_title, book_author, book_dir, md_dir, raw_dir) -> str`(合并路径)
  - 完整 `main(argv=None)`

- [ ] **Step 1: 实现收尾层与完整 main**

追加到 `export.py`,并把 Task 3 的临时 `main` 整体替换为下面版本(`parse_args`/`extract_book_id` 不变):

```python
# ---------- 收尾层:选择持久化 / 图片下载 / 合并 ----------

def load_catalog(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) and data else None
    except Exception:
        return None


def save_selection(path, selected, book_title="", book_author=""):
    rec = {"selected": sorted(selected), "book_title": book_title,
           "book_author": book_author}
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def load_selection(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def prompt_selection(catalog):
    for i, t in enumerate(catalog, 1):
        print(f"  [{i:4d}] {t}")
    while True:
        raw = input(f"\n要导出哪些章节? (共 {len(catalog)} 章, 如 5-12,18, 回车=全部): ").strip()
        if not raw or raw.lower() == "all":
            return set(range(1, len(catalog) + 1))
        sel = parse_chapter_spec(raw, len(catalog))
        if sel:
            return sel
        print("  输入无法解析出任何有效章节, 请重试")


def resolve_selection(spec, selection_path, catalog):
    if spec:
        sel = parse_chapter_spec(spec, len(catalog))
        if not sel:
            sys.exit("❌ --chapters 未解析出任何有效章节")
        return sel
    data = load_selection(selection_path)
    if data and data.get("selected"):
        valid = {i for i in data["selected"] if isinstance(i, int) and 1 <= i <= len(catalog)}
        if valid:
            print(f"  沿用上次选择: {len(valid)} 章 (改选用 --chapters 重新指定)")
            return valid
    return prompt_selection(catalog)


# 强制 IPv4(macOS/部分网络下 IPv6 路由不通会导致每张图卡 ~120s)
_orig_getaddrinfo = socket.getaddrinfo


def _force_ipv4(*a, **k):
    return [x for x in _orig_getaddrinfo(*a, **k) if x[0] == socket.AF_INET]


socket.getaddrinfo = _force_ipv4

_DL_HEADERS = {"Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"}


def _download_one(url, fpath, retries=3):
    if os.path.exists(fpath) and os.path.getsize(fpath) > 1000:
        return "skip"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_DL_HEADERS)
            raw = urllib.request.urlopen(req, timeout=20).read()
            if len(raw) > 500:
                with open(fpath, "wb") as f:
                    f.write(raw)
                return "ok"
        except Exception as e:
            if attempt == retries - 1:
                return f"fail:{e}"
            time.sleep(1.5)
    return "fail:empty"


def download_all_images(raw_dir, img_dir, workers=8):
    os.makedirs(img_dir, exist_ok=True)
    tasks = []
    if os.path.isdir(raw_dir):
        for jf in sorted(os.listdir(raw_dir)):
            if jf.endswith(".json"):
                try:
                    with open(os.path.join(raw_dir, jf), encoding="utf-8") as f:
                        recs = json.load(f).get("images", [])
                except Exception:
                    continue
                for im in recs:
                    tasks.append((im["url"], os.path.join(img_dir, im["file"])))
    if not tasks:
        print("  (无图片)")
        return 0
    print(f"\n  下载 {len(tasks)} 张图片 ({workers} 线程, 强制 IPv4)...")
    ok = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_download_one, u, f): f for u, f in tasks}
        for done, fut in enumerate(as_completed(futs), 1):
            if fut.result().startswith("ok"):
                ok += 1
            elif fut.result().startswith("fail"):
                print(f"    ⚠️  {os.path.basename(futs[fut])}: {fut.result()}")
            if done % 50 == 0:
                print(f"    {done}/{len(tasks)}  ({time.time() - t0:.0f}s)")
    print(f"  ✅ 图片完成 {ok}/{len(tasks)}")
    return ok


def merge_chapters(book_title, book_author, book_dir, md_dir, raw_dir):
    """按目录序合并选中章节(新格式 raw)为 书名.md, 写在书目录内使 images/ 相对路径可用。"""
    recs = []
    if os.path.isdir(raw_dir):
        for jf in os.listdir(raw_dir):
            if jf.endswith(".json"):
                try:
                    with open(os.path.join(raw_dir, jf), encoding="utf-8") as f:
                        r = json.load(f)
                except Exception:
                    continue
                if isinstance(r.get("catalog_idx"), int):
                    recs.append(r)
    recs.sort(key=lambda r: r["catalog_idx"])
    out_path = os.path.join(book_dir, sanitize_filename(book_title) + ".md")
    with open(out_path + ".tmp", "w", encoding="utf-8") as out:
        out.write(f"# {book_title}\n\n**{book_author}**\n\n---\n\n")
        for r in recs:
            md = os.path.join(md_dir, r.get("file", ""))
            if os.path.exists(md):
                with open(md, encoding="utf-8") as f:
                    out.write(f.read())
                out.write("\n\n---\n\n")
    os.replace(out_path + ".tmp", out_path)
    print(f"  📦 合并 {len(recs)} 章 → {out_path}")
    return out_path


async def main(argv=None):
    args = parse_args(argv)
    book_id = extract_book_id(args.book)
    print("=" * 60)
    print("  weread-exporter v4 — 选章 / 标题命名 / 快速")
    print("=" * 60)
    print(f"  Book ID: {book_id}")
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    book_dir = os.path.join("output", book_id)
    md_dir = os.path.join(book_dir, "chapters")
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")
    for d in (md_dir, raw_dir, img_dir):
        os.makedirs(d, exist_ok=True)
    catalog_path = os.path.join(book_dir, "_catalog.json")
    selection_path = os.path.join(book_dir, "_selection.json")

    if args.download_only:
        download_all_images(raw_dir, img_dir)
        return

    catalog = load_catalog(catalog_path)
    if catalog is None:
        print("\n  首次导出: 打开浏览器扫描完整目录...")
        catalog = await scan_catalog_session(book_id, catalog_path)
        print(f"  ✅ 目录共 {len(catalog)} 章")
    if args.scan_only:
        for i, t in enumerate(catalog, 1):
            print(f"  [{i:4d}] {t}")
        return

    selected = resolve_selection(args.chapters, selection_path, catalog)
    book_title = (load_selection(selection_path) or {}).get("book_title", "")
    book_author = (load_selection(selection_path) or {}).get("book_author", "")
    save_selection(selection_path, selected, book_title, book_author)
    print(f"  已选 {len(selected)} 章, 输出目录: {book_dir}")
    name_map = dedup_filenames(catalog)
    seen_imgs = load_seen_imgs(raw_dir)

    errors = 0
    completed = not remaining_work(selected, raw_dir)
    while not completed:
        res = await run_session(book_id, md_dir, raw_dir, catalog, selected,
                                name_map, seen_imgs)
        if res["book_title"]:
            book_title, book_author = res["book_title"], res["book_author"]
            save_selection(selection_path, selected, book_title, book_author)
        if res["error"]:
            errors += 1
            if errors >= 3:
                print("\n  ❌ 连续 3 次会话异常, 停止。已导出内容保留, 重新运行可续传。")
                break
            print("  10 秒后重开浏览器继续...")
            await asyncio.sleep(10)
            continue
        errors = 0
        completed = res["completed"]
        if not completed:
            print("  3 秒后重开继续...")
            await asyncio.sleep(3)

    download_all_images(raw_dir, img_dir)
    merge_chapters(book_title or book_id, book_author, book_dir, md_dir, raw_dir)
    n_ch = len([f for f in os.listdir(md_dir) if f.endswith(".md")])
    print("\n" + "=" * 60)
    if completed:
        print(f"  ✅ 导出完成!  📖 {book_title or book_id} — {book_author}")
    else:
        print("  ⏸  导出未完成, 部分章节已保存。重新运行脚本可从断点续传。")
    print(f"  📄 {n_ch} 个章节文件, 📦 {book_dir}")
    print("=" * 60)
```

- [ ] **Step 2: 回归全部单元测试**

Run: `python -m pytest tests/test_pure.py -v`
Expected: 全部 PASS

- [ ] **Step 3: 端到端冒烟 — 小范围选章导出**

```bash
python export.py e3d32fb0593388e3dde8006kda432420278da4fb5c6e9ad --chapters 1-3
```

Expected: 自动登录→(目录已在 Task 3 扫过则直接复用 `_catalog.json`)→导出 1–3 章→下载该范围图片→`chapters/` 下出现**标题命名**的 3 个 md(如 `扉页.md`)→书目录内生成合并 `书名.md`,图片相对路径可显示。检查 `raw/0001.json`–`0003.json` 含 `catalog_idx` 与 `finished: true`;`_selection.json` 存了 selected 与书名。

- [ ] **Step 4: 冒烟 — 断点续传与续选**

```bash
python export.py e3d32fb0593388e3dde8006kda432420278da4fb5c6e9ad --chapters 1-8
```

运行中途(第 5 章左右)Ctrl+C 杀掉,然后重复同命令。Expected: 重开后打印"剩余 N 章",从断点章**整章**重导(该章 raw 此前为 `finished:false` 已被清理),最终 1–8 章齐全、无半章内容(对比中断章字数应恢复完整)、合并文件含 8 章。再跑一次同命令:不弹浏览器直接进入下载/合并(全部 `finished`)。

- [ ] **Step 5: 冒烟 — 不连续选章 + 交互入口 + --download-only**

```bash
python export.py e3d32fb0593388e3dde8006kda432420278da4fb5c6e9ad --chapters 2,4-5
python export.py e3d32fb0593388e3dde8006kda432420278da4fb5c6e9ad --download-only
```

Expected: 第一条命令导出两段(2 与 4–5),日志出现两次"段"跳转;不传 `--chapters` 直接运行时应沿用 `_selection.json` 不再询问。`--download-only` 不开浏览器只补图。

- [ ] **Step 6: 提交**

```bash
git add export.py
git commit -m "feat: 主流程装配(选择持久化/交互选章/并发下图/按目录序合并)"
```

---

### Task 6: 文档与清理 — README 重写,删除旧脚本

**Files:**
- Modify: `README.md`(全文替换)
- Delete: `export_precise.py`、`download_images.py`

**Interfaces:**
- Consumes: Task 5 的最终 `export.py` 行为

- [ ] **Step 1: 重写 README.md**

```markdown
# weread-exporter

微信读书全本/选章导出工具 — Playwright + Canvas Hook 提取完整**图文**内容,导出为按**章节标题命名**的 Markdown。支持选择章节、断点续传、图文按阅读顺序精确交错。

## 原理

微信读书网页版用 Canvas 渲染文字(而非 DOM 文本),插图是 DOM `<img>`。本工具:

1. **Playwright 自动化** — 持久化登录(扫码一次,后续复用)
2. **Canvas fillText Hook** — 拦截每次文字绘制,捕获字符 (x, y) 坐标
3. **目录全量扫描** — 增量滚动目录面板,收集全部章节并编号
4. **选章导出** — `--chapters` 或交互选择;目录点击定点跳转,分段导出
5. **图文交错** — 文字行与视口内图片按屏幕 y 坐标排序,插图落在正确段落间
6. **自适应翻页** — 渲染稳定即翻页(约 0.3–0.6 秒/页),卡死自动重开续传

## 安装

```bash
pip install -r requirements.txt   # playwright + pytest
playwright install chromium
```

## 使用

```bash
# 交互式:打开书 → 列出目录 → 输入范围(如 5-12,18)或回车全选
python export.py <reader_url_or_book_id>

# 指定章节(1-based 目录编号)
python export.py <book_id> --chapters 5-12,18

# 只扫描目录不导出
python export.py <book_id> --scan-only

# 只补下缺失图片(不开浏览器)
python export.py <book_id> --download-only
```

- 首次运行弹浏览器扫码,会话存 `cache/browser_profile/` 自动复用
- 中途中断(崩溃/Ctrl+C)后**重跑同一命令**即续传;半章会整章重导,不丢内容
- 重跑已完成的书不弹浏览器,直接补图+合并

## 输出

```
output/<book_id>/
├── _catalog.json        # 目录标题数组(位置即编号)
├── _selection.json      # 上次选章与书名(续传/合并用)
├── chapters/第1章 xxx.md       # 章节按标题命名(重复标题自动加 (2))
├── images/              # chXXXX_imgNN.jpg 并发下载
├── raw/                 # 每章元数据(catalog_idx/finished/图片URL)
└── 书名.md              # 选中章节按目录序合并
```

> v3 旧版导出目录(数字命名 md)与新格式不互通:旧 raw 无 `catalog_idx` 会被忽略,建议旧目录改名备份后重新导出。

## 限制

- 需微信读书账号且对目标书有阅读权限
- 部分出版社限制网页端阅读("去 App 阅读"),无法导出
- 纯图廊章节图注配对偶尔差一位;正文图片位置准确

## 声明

仅供个人学习研究使用。请勿用于商业用途或大规模传播,请尊重著作权。
```

- [ ] **Step 2: 删除旧脚本并全量回归**

```bash
git rm export_precise.py download_images.py
python -m pytest tests/test_pure.py -v
python export.py --help
```

Expected: 测试全 PASS;`--help` 正常显示用法。

- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs: README 更新为 v4 用法;移除 v3 旧脚本"
```

---

## Self-Review 记录

- **Spec 覆盖**:标题命名(T1/T4 `dedup_filenames`+`save_chapter`)、选章双通道(T1 `parse_chapter_spec`+T5 `resolve_selection`/`prompt_selection`)、目录扫描与跳转(T3)、不连续段跳转(T4 `compute_segments`+`export_segment`)、提速(T4 `wait_render_stable`)、断点续传(T4 `finished`/`purge`/T5 会话循环)、图片并发下载(T5)、合并全本(T5)、README/旧文件清理(T6)——全覆盖;规格修正 4 条已在计划头锁定
- **占位符**:无 TBD/TODO;移植步骤给出精确行号与签名,新代码全部内联
- **类型一致性**:`catalog_idx` 全程 1-based int;`name_map` 为 `dedup_filenames(catalog)` 输出按 `catalog_idx-1` 索引;`export_segment`/`run_session`/`main` 的参数名与调用点一致;raw json 字段与 `remaining_work`/`merge_chapters`/`load_seen_imgs` 读取键一致
