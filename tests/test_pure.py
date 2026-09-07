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
