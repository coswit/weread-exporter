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
        assert match_catalog_title(self.TITLES, "注释", 3) == 5

    def test_containment_fallback(self):
        assert match_catalog_title(["推荐序一"], "推荐序", 0) == 1

    def test_whitespace_normalized(self):
        assert match_catalog_title([" 第1章 开始 "], "第1章 开始", 0) == 1

    def test_not_found(self):
        assert match_catalog_title(self.TITLES, "不存在的章", 0) is None

    def test_empty_header(self):
        assert match_catalog_title(self.TITLES, "", 0) is None


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
        chars = [_ch("界", 20, 100), _ch("世", 10, 99), _ch("好", 10, 201), _ch("人", 20, 200)]
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


from export import locate_slice  # noqa: E402


class TestLocateSlice:
    def test_overlap_found(self):
        titles = ["a", "b", "c", "d", "e"]
        assert locate_slice(titles, ["c", "d"]) == 2

    def test_no_match(self):
        assert locate_slice(["a", "b"], ["x"]) is None

    def test_full_match_at_zero(self):
        assert locate_slice(["a", "b"], ["a", "b"]) == 0


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


from export import drop_overlap_blocks, prev_last_paragraph  # noqa: E402


class TestDropOverlapBlocks:
    def test_drops_suffix_lines(self):
        blocks = [{"type": "text", "text": "前章末行一"},
                  {"type": "text", "text": "前章末行二"},
                  {"type": "text", "text": "本章新内容"}]
        n = drop_overlap_blocks(blocks, "更早的内容前章末行一前章末行二")
        assert n == 2
        assert [b["text"] for b in blocks] == ["本章新内容"]

    def test_stops_at_image(self):
        blocks = [{"type": "img", "src": "u", "w": 1, "h": 1},
                  {"type": "text", "text": "前章末行"}]
        assert drop_overlap_blocks(blocks, "xx前章末行") == 0

    def test_no_overlap_untouched(self):
        blocks = [{"type": "text", "text": "完全不同的开头"}]
        assert drop_overlap_blocks(blocks, "另一个段落") == 0
        assert len(blocks) == 1

    def test_empty_prev(self):
        blocks = [{"type": "text", "text": "x"}]
        assert drop_overlap_blocks(blocks, "") == 0


class TestPrevLastParagraph:
    def test_reads_last_text_para(self, tmp_path):
        raw, md = tmp_path / "raw", tmp_path / "chapters"
        raw.mkdir(); md.mkdir()
        (raw / "0005.json").write_text(
            json.dumps({"file": "第5章.md"}), encoding="utf-8")
        (md / "第5章.md").write_text(
            "# 第5章\n\n开头段。\n\n![图](images/a.jpg)\n\n结尾段落。", encoding="utf-8")
        assert prev_last_paragraph(str(raw), str(md), 6) == "结尾段落。"

    def test_missing_returns_empty(self, tmp_path):
        assert prev_last_paragraph(str(tmp_path / "raw"), str(tmp_path / "md"), 6) == ""
        assert prev_last_paragraph(str(tmp_path), str(tmp_path), 1) == ""


from export import heading_match, trim_to_heading  # noqa: E402


class TestHeadingMatch:
    T = ["扉页", "1.1 系统架构", "1.1.1 Android系统架构", "1.1.2 本书的架构"]

    def test_exact_next(self):
        assert heading_match(self.T, "1.1.1 Android系统架构", 2) == 3

    def test_whitespace_normalized(self):
        assert heading_match(self.T, " 1.1  系统架构 ", 1) == 2

    def test_window_limit(self):
        assert heading_match(self.T, "1.1.2 本书的架构", 1) is None  # 距离 3 超窗

    def test_body_text_no_match(self):
        assert heading_match(self.T, "Android系统采用分层架构", 1) is None


class TestTrimToHeading:
    def test_trims_up_to_heading_inclusive(self):
        blocks = [{"type": "text", "text": "上一小节的尾巴"},
                  {"type": "text", "text": "1.1.1 Android系统架构"},
                  {"type": "text", "text": "本节正文。"}]
        assert trim_to_heading(blocks, "1.1.1 Android系统架构") == 2
        assert [b["text"] for b in blocks] == ["本节正文。"]

    def test_image_first_no_trim(self):
        blocks = [{"type": "img", "src": "u", "w": 1, "h": 1},
                  {"type": "text", "text": "1.1 系统架构"}]
        assert trim_to_heading(blocks, "1.1 系统架构") == 0
        assert len(blocks) == 2

    def test_no_match_no_trim(self):
        blocks = [{"type": "text", "text": "正文"}]
        assert trim_to_heading(blocks, "不存在的标题") == 0
