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
            out.append(f"![图](images/{fname})")
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
