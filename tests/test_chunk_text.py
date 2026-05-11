"""
test_chunk_text.py — chunk_text.py 단위 테스트

테스트 대상:
- find_chunk_boundary(): 섹션 경계 우선 청킹
- chunk_file(): MIN_CHUNK / MAX_CHUNK 범위 내 분할
- chunk_file(): 빈 파일 처리
- read_file_content(): txt 파일 읽기
- extract_abbreviations(): 약어 파싱
"""
import sys
import tempfile
from pathlib import Path

import pytest


from wiki_builder.chunk_text import (
    find_chunk_boundary,
    chunk_file,
    read_file_content,
    extract_abbreviations,
)


# ──────────────────────────────────────────────
# find_chunk_boundary 테스트
# ──────────────────────────────────────────────

class TestFindChunkBoundary:
    def _make_text_with_sections(self, sections=None, body_size=100):
        """섹션 헤더를 가진 텍스트 생성."""
        parts = []
        secs = sections or [("6", "General"), ("6.1", "Sub-section")]
        for num, title in secs:
            parts.append(f"\n{num}\t{title}\n" + "x" * body_size)
        return "".join(parts)

    def test_section_boundary_preferred(self):
        """min_size와 max_size 사이에 섹션 경계가 있으면 그 위치 반환.

        Note: max_size가 텍스트 길이보다 크면 early return으로 len(text)를 반환.
        따라서 max_size < len(text) 조건을 맞춰야 한다.
        """
        # 텍스트를 충분히 길게 만들어 early return 방지
        prefix = "x" * 100
        section = "\n6.1\tSub section\n"
        suffix = "y" * 1000  # 충분히 길게
        text = prefix + section + suffix  # len > max_size=300

        boundary = find_chunk_boundary(text, start=0, min_size=50, max_size=300)
        # 섹션 위치: 100 >= min_size(50) → 섹션 경계 반환
        assert boundary == 100  # \n6.1\t 패턴 시작 위치

    def test_no_section_boundary_returns_max(self):
        """범위 내 섹션 경계 없으면 start + max_size 반환."""
        text = "a" * 1000
        boundary = find_chunk_boundary(text, start=0, min_size=100, max_size=200)
        assert boundary == 200

    def test_end_of_text_returns_len(self):
        """텍스트 끝에 도달하면 len(text) 반환."""
        text = "short"
        boundary = find_chunk_boundary(text, start=0, min_size=10, max_size=50)
        assert boundary == len(text)

    def test_boundary_at_correct_section_level(self):
        """섹션 번호 패턴(\\n\\d+(\\.\\d+)*\\t) 정확히 감지."""
        text = "x" * 50 + "\n7\tNew Chapter\n" + "y" * 200
        boundary = find_chunk_boundary(text, start=0, min_size=30, max_size=150)
        # \n7\t 위치: 50
        assert boundary == 50

    def test_search_starts_after_min_size(self):
        """min_size 이전의 섹션 경계는 무시."""
        # min_size=100, 섹션이 50 위치에 있으면 무시
        text = "x" * 20 + "\n6.1\tEarly section\n" + "y" * 80 + "\n6.2\tLate section\n" + "z" * 200
        boundary = find_chunk_boundary(text, start=0, min_size=100, max_size=300)
        # 50 위치의 섹션은 min_size=100 이전 → 무시
        # 100 이후 첫 섹션: "Late section" 위치
        assert boundary > 50


# ──────────────────────────────────────────────
# chunk_file 테스트
# ──────────────────────────────────────────────

class TestChunkFile:
    def test_empty_file_returns_empty_list(self, tmp_path):
        """빈 파일 → 빈 청크 목록."""
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        chunks = chunk_file(str(f), min_size=100, max_size=200)
        assert chunks == []

    def test_small_file_returns_one_chunk(self, tmp_path):
        """파일이 min_size보다 작으면 1개 청크."""
        f = tmp_path / "small.txt"
        f.write_text("작은 텍스트 내용입니다." * 5, encoding="utf-8")
        chunks = chunk_file(str(f), min_size=10000, max_size=50000)
        assert len(chunks) == 1

    def test_chunk_has_required_fields(self, tmp_path):
        """각 청크는 index, text, start, end 필드를 가진다."""
        f = tmp_path / "test.txt"
        f.write_text("내용" * 100, encoding="utf-8")
        chunks = chunk_file(str(f), min_size=50, max_size=200)
        for chunk in chunks:
            assert "index" in chunk
            assert "text" in chunk
            assert "start" in chunk
            assert "end" in chunk

    def test_chunks_cover_full_content(self, tmp_path):
        """청크들이 전체 내용을 커버한다."""
        content = "내용" * 500
        f = tmp_path / "test.txt"
        f.write_text(content, encoding="utf-8")
        chunks = chunk_file(str(f), min_size=100, max_size=200)

        # 청크 start가 단조 증가
        for i in range(1, len(chunks)):
            assert chunks[i]["start"] == chunks[i-1]["end"]

        # 마지막 청크가 끝까지 커버
        assert chunks[-1]["end"] == len(content)

    def test_chunk_indices_sequential(self, tmp_path):
        """청크 index는 0부터 순차적으로 증가."""
        content = "x" * 1000
        f = tmp_path / "test.txt"
        f.write_text(content, encoding="utf-8")
        chunks = chunk_file(str(f), min_size=100, max_size=200)

        for i, chunk in enumerate(chunks):
            assert chunk["index"] == i

    def test_large_file_creates_multiple_chunks(self, tmp_path):
        """큰 파일은 여러 청크로 분할된다."""
        content = "내용입니다. " * 10000  # 큰 파일
        f = tmp_path / "large.txt"
        f.write_text(content, encoding="utf-8")
        chunks = chunk_file(str(f), min_size=500, max_size=1000)
        assert len(chunks) > 1

    def test_whitespace_only_chunk_skipped(self, tmp_path):
        """공백만 있는 청크는 제외."""
        content = "\n\n\n\n" + "내용" * 100
        f = tmp_path / "test.txt"
        f.write_text(content, encoding="utf-8")
        chunks = chunk_file(str(f), min_size=10, max_size=500)
        for chunk in chunks:
            assert chunk["text"].strip() != ""


# ──────────────────────────────────────────────
# read_file_content 테스트
# ──────────────────────────────────────────────

class TestReadFileContent:
    def test_txt_file_read(self, tmp_path):
        f = tmp_path / "test.txt"
        content = "테스트 내용\n두 번째 줄"
        f.write_text(content, encoding="utf-8")
        result = read_file_content(str(f))
        assert "테스트 내용" in result

    def test_txt_with_section_headers(self, tmp_path):
        """txt 파일의 섹션 헤더(탭 구분)가 그대로 읽힌다."""
        content = "일반 내용\n섹션 내용"
        f = tmp_path / "test.txt"
        f.write_text(content, encoding="utf-8")
        result = read_file_content(str(f))
        assert result == content


# ──────────────────────────────────────────────
# extract_abbreviations 테스트
# ──────────────────────────────────────────────

class TestExtractAbbreviations:
    def _make_spec_text(self, abbrevs):
        """3GPP 스펙 약어 섹션 텍스트 생성."""
        lines = ["\n3.3\tAbbreviations\n"]
        for abbr, expansion in abbrevs.items():
            lines.append(f"{abbr}  {expansion}\n")  # 줄바꿈 포함
        lines.append("\n4\tGeneral\n")
        return "".join(lines)

    def test_simple_abbreviation_parsed(self):
        text = self._make_spec_text({"PUSCH": "Physical Uplink Shared Channel"})
        abbrevs = extract_abbreviations(text)
        assert "PUSCH" in abbrevs
        assert "Physical Uplink Shared Channel" in abbrevs["PUSCH"]

    def test_multiple_abbreviations(self):
        text = self._make_spec_text({
            "PUSCH": "Physical Uplink Shared Channel",
            "PDSCH": "Physical Downlink Shared Channel",
            "UCI": "Uplink Control Information",
        })
        abbrevs = extract_abbreviations(text)
        assert "PUSCH" in abbrevs
        assert "PDSCH" in abbrevs
        assert "UCI" in abbrevs

    def test_no_section_returns_empty(self):
        """3.3 섹션 없으면 빈 dict."""
        text = "일반 텍스트\n4\tGeneral\n내용"
        abbrevs = extract_abbreviations(text)
        assert abbrevs == {}

    def test_lowercase_words_not_parsed(self):
        """소문자 단어는 약어가 아니므로 포함되지 않음."""
        text = "\n3.3\tAbbreviations\nthis is not an abbreviation  some expansion\n\n4\tGeneral\n"
        abbrevs = extract_abbreviations(text)
        assert "this" not in abbrevs

    def test_boundary_at_section_4(self):
        """섹션 4 시작 전까지만 파싱."""
        text = "\n3.3\tAbbreviations\nPUSCH  Physical Uplink Shared Channel\n\n4\tGeneral\nHARQ  Hybrid ARQ"
        abbrevs = extract_abbreviations(text)
        assert "PUSCH" in abbrevs
        assert "HARQ" not in abbrevs  # 4절 이후는 파싱 안 함
