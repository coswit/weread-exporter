#!/usr/bin/env python3
"""weread-exporter v4 — 微信读书导出：章节选择/标题命名/快速翻页。

在 v3（Canvas Hook 图文捕获）基础上重写流程层：
目录全量扫描 → 选章（--chapters 或交互） → 目录点击跳转分段导出
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


# ---------- 纯函数：选章解析 / 文件名 ----------

def parse_chapter_spec(spec, total):
    """解析 "5-12,18" → {5..12, 18}（1-based）。非法片段警告跳过，越界截断/丢弃。"""
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
    """Windows 安全文件名：非法字符→_，去首尾空白与结尾点号，截断。"""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    s = s.rstrip(". ")
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    return s or "_"


def dedup_filenames(titles):
    """全书目录 → 每章唯一文件名（不含扩展名）；重复标题追加 (2)(3)...（按目录序）。"""
    seen = {}
    out = []
    for t in titles:
        base = sanitize_filename(t)
        n = seen.get(base, 0) + 1
        seen[base] = n
        out.append(base if n == 1 else f"{base} ({n})")
    return out


def clean_catalog_title(text):
    """剥离目录条目混入的进度文字（如「版权信息当前读到 60%+书签」→「版权信息」）。"""
    s = re.sub(r"当前读到.*$", "", text)
    s = re.sub(r"\+?\s*书签\s*$", "", s)
    return s.strip()


def match_catalog_title(titles, header, from_pos):
    """顶部标题 → 目录编号（1-based）；从 from_pos+1 向后搜索。
    先去空白全等，再互相包含（兜住目录标题剥离不净的情况）。找不到返回 None。"""
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


CANVAS_HOOK = """
(function() {
    window.__wr_chars = [];
    var origFill = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (text && text.trim())
            window.__wr_chars.push({t: text, x: Math.round(x*10)/10, y: Math.round(y*10)/10});
        return origFill.apply(this, arguments);
    };
    window.__wr_reset = function() { window.__wr_chars = []; };
    window.__wr_count = function() { return window.__wr_chars.length; };
})();
"""

# 当前视口内可见的书籍插图，带屏幕坐标
VIEWPORT_IMGS_JS = """
() => {
    const H = window.innerHeight, W = window.innerWidth, out = [];
    document.querySelectorAll('img[class*="wr_readerImage"]').forEach(i => {
        const src = i.src || i.getAttribute('data-src') || '';
        if (!src.includes('res.weread.qq.com/wrepub')) return;
        const r = i.getBoundingClientRect();
        if (r.width > 40 && r.height > 40 && r.bottom > 0 && r.top < H &&
            r.right > 0 && r.left < W &&
            getComputedStyle(i).visibility !== 'hidden' &&
            getComputedStyle(i).display !== 'none') {
            out.push({src, top: Math.round(r.top), left: Math.round(r.left),
                      w: i.naturalWidth||i.width, h: i.naturalHeight||i.height});
        }
    });
    return out;
}
"""

# reader 的两个 canvas 的屏幕位置
CANVAS_RECTS_JS = """
() => Array.from(document.querySelectorAll('canvas')).map(c => {
    const r = c.getBoundingClientRect();
    return {top: r.top, left: r.left, w: Math.round(r.width), h: Math.round(r.height)};
}).filter(r => r.h > 300)
"""

MEASURE_RE = re.compile(r'^[a-zA-Z0-9`~!@#$%^&*()\-_=+\[\]{}|;:\',<.>/?\\"\s]+$')
SENTENCE_END = set("。！？；：」）】》…—")


def split_spread(chars):
    """双页拆分：返回 [左页chars, 右页chars] 或 [单页chars]"""
    if len(chars) < 20:
        return [chars]
    singles = [(i, c) for i, c in enumerate(chars) if len(c["t"]) == 1]
    if len(singles) < 10:
        return [chars]
    for j in range(1, len(singles)):
        if singles[j - 1][1]["y"] > 400 and singles[j][1]["y"] < 200:
            return [chars[:singles[j][0]], chars[singles[j][0]:]]
    return [chars]


def chars_to_lines(chars):
    """把单页字符按 y 分行，返回 [{y, text}]（未合并段落）"""
    real = [c for c in chars if len(c["t"]) == 1 or not MEASURE_RE.match(c["t"])]
    if not real:
        return []
    rows = {}
    for c in real:
        y_key = round(c["y"] / 3) * 3
        rows.setdefault(y_key, []).append(c)
    lines = []
    for yk in sorted(rows):
        line = "".join(c["t"] for c in sorted(rows[yk], key=lambda c: c["x"]))
        if line.strip():
            lines.append({"y": yk, "text": line.strip()})
    return lines


def build_page_blocks(chars, images, canvas_rects, seen_imgs):
    """把一次渲染(可能双页)拆成有序块列表: [{type:'text'/'img', ...}]
       文字行和图片按屏幕 y 交错；左页整页在前，右页在后。"""
    blocks = []
    pages = split_spread(chars)

    # 判定左右 canvas
    rects = sorted(canvas_rects, key=lambda r: r["left"])
    left_rect = rects[0] if rects else {"top": 0, "left": 0}
    right_rect = rects[1] if len(rects) > 1 else left_rect
    mid_x = (left_rect["left"] + right_rect["left"]) / 2 + 180 if len(rects) > 1 else 99999

    # 图片按左右分组
    left_imgs = [im for im in images if im["left"] < mid_x]
    right_imgs = [im for im in images if im["left"] >= mid_x]

    def emit_page(page_chars, page_rect, page_imgs):
        lines = chars_to_lines(page_chars)
        items = []
        for ln in lines:
            items.append(("text", page_rect["top"] + ln["y"], ln["text"]))
        for im in page_imgs:
            if im["src"] in seen_imgs:
                continue
            items.append(("img", im["top"], im))
        items.sort(key=lambda t: t[1])
        for typ, _y, payload in items:
            if typ == "text":
                blocks.append({"type": "text", "text": payload})
            else:
                seen_imgs.add(payload["src"])
                blocks.append({"type": "img", "src": payload["src"],
                                "w": payload["w"], "h": payload["h"]})

    if len(pages) == 2:
        emit_page(pages[0], left_rect, left_imgs)
        emit_page(pages[1], right_rect, right_imgs)
    else:
        # 单页：图片全归这页，仍按 y 排
        emit_page(pages[0], left_rect, left_imgs + right_imgs)
    return blocks


def img_filename(url, ch_idx, seq):
    ext = "jpg"
    m = re.search(r'\.(jpg|jpeg|png|gif|webp)', url.lower())
    if m:
        ext = m.group(1).replace("jpeg", "jpg")
    return f"ch{ch_idx:04d}_img{seq:02d}.{ext}"


def render_chapter_md(ch_title, blocks, ch_idx):
    """把有序块渲染成 Markdown：文字行合并成段落，图片就地插入"""
    out = [f"# {ch_title}\n"]
    para = []
    img_records = []
    img_seq = 0

    def flush_para():
        nonlocal para
        if not para:
            return
        # 合并 canvas 断行为自然段：上一行不以句末标点结尾则接续
        merged = []
        for line in para:
            if line == ch_title:
                continue
            if merged and merged[-1] and merged[-1][-1] not in SENTENCE_END:
                merged[-1] += line
            else:
                merged.append(line)
        for m in merged:
            if m.strip():
                out.append(m.strip())
        para = []

    for b in blocks:
        if b["type"] == "text":
            para.append(b["text"])
        else:
            flush_para()
            img_seq += 1
            fname = img_filename(b["src"], ch_idx, img_seq)
            out.append(f"![图](./images/{fname})")
            img_records.append({"url": b["src"], "file": fname})
    flush_para()

    body = "\n\n".join(out) + "\n"
    return body, img_records


def kill_stale_browsers():
    """清掉仍占用本工具 profile 的残留浏览器进程。
    浏览器被强关/崩溃后常留下孤儿进程锁住 profile，导致下次 launch 直接失败。"""
    marker = os.path.abspath(USER_DATA_DIR)
    try:
        if sys.platform == "win32":
            ps_cmd = (
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "Where-Object {$_.CommandLine -like '*" + marker + "*'} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                "-ErrorAction SilentlyContinue }"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                           capture_output=True, timeout=30)
        else:
            subprocess.run(["pkill", "-f", marker], capture_output=True, timeout=30)
    except Exception:
        pass


# ---------- 目录浏览器层 ----------

# 目录条目（清理前的原始文本，DOM 序）
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
    """当前渲染切片（虚拟列表只渲染可见窗口）在完整目录中的起始下标。
    返回最小的 o 使 titles[o:o+len(visible)] == visible；找不到返回 None。"""
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
    """打开目录面板，小步滚动到底，按序收集全部章节标题（虚拟列表增量渲染）。"""
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
    """打开目录，滚动到第 target_idx（1-based）章条目并点击跳转。失败抛 RuntimeError。"""
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
            raise RuntimeError("登录超时（10 分钟）")
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


def _save_empty_chapter(ch_title, catalog_idx, md_dir, raw_dir, name_map):
    """无任何可捕获内容的章节(如纯封面扉页): 记空章为 finished, 防续传死循环。"""
    fname = name_map[catalog_idx - 1] + ".md"
    md_path = os.path.join(md_dir, fname)
    with open(md_path + ".tmp", "w", encoding="utf-8") as f:
        f.write(f"# {ch_title}\n")
    os.replace(md_path + ".tmp", md_path)
    rec = {"title": ch_title, "catalog_idx": catalog_idx, "file": fname,
           "images": [], "text_len": 0, "finished": True}
    raw_path = os.path.join(raw_dir, f"{catalog_idx:04d}.json")
    with open(raw_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(raw_path + ".tmp", raw_path)
    print(f"  [{catalog_idx:4d}] {ch_title[:36]:36s}      0字 [无可捕获内容, 记为空章]")


def prev_last_paragraph(raw_dir, md_dir, start_idx):
    """读取 start_idx-1 章落盘 md 的最后一个纯文字段落, 供跨界去重;取不到返回空串。"""
    if start_idx < 2:
        return ""
    raw_path = os.path.join(raw_dir, f"{start_idx - 1:04d}.json")
    try:
        with open(raw_path, encoding="utf-8") as f:
            fname = json.load(f).get("file", "")
    except Exception:
        return ""
    md_path = os.path.join(md_dir, fname)
    if not fname or not os.path.exists(md_path):
        return ""
    try:
        with open(md_path, encoding="utf-8") as f:
            paras = [p.strip() for p in f.read().split("\n\n") if p.strip()]
    except Exception:
        return ""
    for p in reversed(paras):
        if not p.startswith("#") and not p.startswith("!["):
            return p
    return ""


def drop_overlap_blocks(blocks, prev_last_para):
    """跳转落地页与前章末页跨界的文字去重: 丢弃开头连续文字块, 只要其累加文本
    是前一章最后段落的后缀(落地页左半渲染的是前章尾部)。返回丢弃的块数。"""
    if not prev_last_para:
        return 0
    committed = 0
    acc = ""
    n = 0
    while n < len(blocks) and blocks[n]["type"] == "text":
        cand = acc + blocks[n]["text"]
        if prev_last_para.endswith(cand):
            committed = n + 1  # 累加文本构成前章末段后缀, 确认丢弃
            acc = cand
            n += 1
        elif cand in prev_last_para:
            # 中间累加未必恰好是后缀, 但仍是潜在跨界内容, 暂记继续观察
            acc = cand
            n += 1
        else:
            break
    del blocks[:committed]
    return committed


class _SegmentEnd(Exception):
    """页内切分命中段外章节时, 用于从捕获流程中跳出本段。"""


def heading_match(titles, text, cur_pos, window=2):
    """画布文字行是否恰为 cur_pos 前方 window 内某个目录标题(去空白全等)。
    返回命中的目录编号(1-based)或 None。用于页内小节边界检测。"""
    n = re.sub(r"\s+", "", text or "")
    if not n:
        return None
    for idx in range(cur_pos + 1, min(cur_pos + 1 + window, len(titles) + 1)):
        if re.sub(r"\s+", "", titles[idx - 1]) == n:
            return idx
    return None


def trim_to_heading(blocks, title):
    """丢弃开头块直到命中本章标题行(含该行);先遇到图片或未命中则不裁。
    返回丢弃的块数。用于跳转/切章后落地页含前一小节内容的裁剪。"""
    n = re.sub(r"\s+", "", title or "")
    if not n:
        return 0
    for i, b in enumerate(blocks):
        if b["type"] == "img":
            return 0
        if b["type"] == "text" and re.sub(r"\s+", "", b["text"]) == n:
            del blocks[:i + 1]
            return i + 1
    return 0


async def export_segment(page, seg, titles, selected, md_dir, raw_dir, name_map, seen_imgs):
    """导出一个连续章节段 seg(1-based 目录编号列表)。返回 (保存章数, 结束原因)。"""
    start, end = seg[0], seg[-1]
    print(f"  ▶ 段 {start}-{end}: 跳到「{titles[start - 1]}」")
    await jump_to_chapter(page, start, titles)

    cur_pos = start
    cur_title = titles[start - 1]
    h = await header_title(page)
    m = match_catalog_title(titles, h, start - 1) if h else None
    if m is not None and m > start:
        if m - start > 3:
            raise RuntimeError(f"跳转落点第 {m} 章与目标第 {start} 章相差过大, 判定跳转失败")
        # 阅读器跳过封面/扉页等无正文页: 被跳过的选中章记为空章, 防续传死循环
        for skipped in range(start, m):
            if skipped in selected:
                _save_empty_chapter(titles[skipped - 1], skipped, md_dir, raw_dir, name_map)
        cur_pos = m
        cur_title = titles[m - 1]
    elif m is None and h:
        print(f"  ℹ️  跳转后顶部标题暂为「{h}」(未及更新), 以目录第 {start} 章为准")
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
        nonlocal cur_pos, cur_title
        await asyncio.sleep(0.15)
        chars = await page.evaluate("() => window.__wr_chars")
        rects = await page.evaluate(CANVAS_RECTS_JS)
        imgs = await page.evaluate(VIEWPORT_IMGS_JS)
        added = 0
        cut = False
        for b in build_page_blocks(chars, imgs, rects, seen_imgs):
            if (b["type"] == "text" and blocks
                    and blocks[-1].get("type") == "text"
                    and blocks[-1]["text"] == b["text"]):
                continue
            if not cut and b["type"] == "text":
                hm = heading_match(titles, b["text"], cur_pos)
                if hm is not None:
                    cut = True
                    if not blocks and cur_pos in selected:
                        _save_empty_chapter(cur_title, cur_pos, md_dir, raw_dir, name_map)
                    save_and_clear()
                    if hm > end or hm not in selected:
                        # 顶部/画布标题直接跳到段外: 中间被跨过的选中章多为
                        # 不足一页的小节(内容已并入前一章页面), 记空章防重复导出
                        for skipped in range(cur_pos + 1, hm):
                            if skipped in selected:
                                _save_empty_chapter(titles[skipped - 1], skipped,
                                                    md_dir, raw_dir, name_map)
                        raise _SegmentEnd()
                    cur_pos, cur_title = hm, titles[hm - 1]
                    continue  # 标题行本身不进正文
            blocks.append(b)
            added += 1
        return added

    # 首页
    await page.evaluate("() => window.__wr_reset()")
    await wait_render_stable(page)
    await capture_once()
    trimmed = trim_to_heading(blocks, cur_title)
    if trimmed:
        print(f"  ℹ️  页内定位: 裁去本章标题行前的 {trimmed} 块")
    dropped = drop_overlap_blocks(blocks, prev_last_paragraph(raw_dir, md_dir, start))
    if dropped:
        print(f"  ℹ️  跨界去重: 丢弃与前章末页重复的 {dropped} 行")

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
                    if not blocks and cur_pos in selected:
                        _save_empty_chapter(cur_title, cur_pos, md_dir, raw_dir, name_map)
                    save_and_clear()
                    if m > end or m not in selected:
                        for skipped in range(cur_pos + 1, m):
                            if skipped in selected:
                                _save_empty_chapter(titles[skipped - 1], skipped,
                                                    md_dir, raw_dir, name_map)
                        return saved[0], "segment_end"
                    cur_pos, cur_title = m, new_title
                    await capture_once()  # 新章首页
                    trim_to_heading(blocks, cur_title)
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
    except _SegmentEnd:
        return saved[0], "segment_end"
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
            r = fut.result()
            if r.startswith("ok") or r == "skip":
                ok += 1  # 成功+跳过(计划 Interfaces: 返回 成功+跳过数)
            elif r.startswith("fail"):
                print(f"    ⚠️  {os.path.basename(futs[fut])}: {r}")
            if done % 50 == 0:
                print(f"    {done}/{len(tasks)}  ({time.time() - t0:.0f}s)")
    print(f"  ✅ 图片完成 {ok}/{len(tasks)}")
    return ok


def merge_chapters(book_title, book_author, book_dir, md_dir, raw_dir):
    """按目录序合并选中章节(新格式 raw)为 书名.md, 写在书目录内。
    章节 md 引用 ./images/, 合并文件在上一级, 改写为 ./chapters/images/。"""
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
                    body = f.read()
                # 章节 md 里的 ./images/ 或旧版 images/ → 相对合并文件的路径
                body = re.sub(r"\]\((\./)?images/", "](./chapters/images/", body)
                out.write(body)
                out.write("\n\n---\n\n")
    os.replace(out_path + ".tmp", out_path)
    print(f"  📦 合并 {len(recs)} 章 → {out_path}")
    return out_path


# ---------- CLI ----------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="weread-exporter v4 — 微信读书导出（选章/标题命名/快速）")
    ap.add_argument("book", help="reader URL 或 book_id")
    ap.add_argument("--chapters", default=None,
                    help='要导出的章节, 如 "5-12,18"（1-based；不传则交互选择/沿用上次）')
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
    md_dir = os.path.join(book_dir, "chapters")
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(md_dir, "images")  # 放 chapters/ 内, 章节 md 用 ./images/ 直接触达
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
    stalled = 0
    completed = not remaining_work(selected, raw_dir)
    while not completed:
        before = len(remaining_work(selected, raw_dir))
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
            if len(remaining_work(selected, raw_dir)) >= before:
                stalled += 1
                if stalled >= 2:
                    stuck = sorted(remaining_work(selected, raw_dir))
                    print(f"\n  ⏸  连续 {stalled} 次会话无进展, 停止。未导出章节: {stuck}")
                    break
            else:
                stalled = 0
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


if __name__ == "__main__":
    asyncio.run(main())
