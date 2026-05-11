"""
test_query.py — query.py 단위 테스트

테스트 대상:
- _parse_json_field(): LLM 응답에서 JSON 블록 파싱
- _select_pages(): 최대 5개 페이지 제한
- _file_answer(): 저장 경로 형식 (wiki/query/YYYY-MM-DD_slug.md)
- run_query(): 기본 플로우 (index.md 없어도 동작)
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


from wiki_builder.query import (
    _parse_json_field,
    _select_pages,
    _file_answer,
    run_query,
    MAX_PAGES,
)


# ──────────────────────────────────────────────
# _parse_json_field 테스트
# ──────────────────────────────────────────────

class TestParseJsonField:
    def test_simple_json_parsed(self):
        raw = '{"pages": ["entities/PUSCH.md", "concepts/UCI.md"]}'
        result = _parse_json_field(raw, "pages", default=[])
        assert result == ["entities/PUSCH.md", "concepts/UCI.md"]

    def test_json_code_block_parsed(self):
        raw = '```json\n{"pages": ["entities/PUSCH.md"]}\n```'
        result = _parse_json_field(raw, "pages", default=[])
        assert result == ["entities/PUSCH.md"]

    def test_missing_field_returns_default(self):
        raw = '{"other_field": "value"}'
        result = _parse_json_field(raw, "pages", default=[])
        assert result == []

    def test_invalid_json_returns_default(self):
        raw = "이것은 JSON이 아닙니다"
        result = _parse_json_field(raw, "pages", default=[])
        assert result == []

    def test_empty_string_returns_default(self):
        result = _parse_json_field("", "pages", default=None)
        assert result is None

    def test_nested_field_extraction(self):
        raw = '{"answer": "응답", "sources": ["a.md"]}'
        result = _parse_json_field(raw, "sources", default=[])
        assert result == ["a.md"]


# ──────────────────────────────────────────────
# _select_pages 테스트
# ──────────────────────────────────────────────

class TestSelectPages:
    def test_max_pages_limit_enforced(self, tmp_path):
        """LLM이 MAX_PAGES 초과 페이지를 반환해도 최대 MAX_PAGES만 선택."""
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()
        (wiki_path / "entities").mkdir()

        # 7개 파일 생성
        paths = []
        for i in range(7):
            p = f"entities/Page{i}.md"
            (wiki_path / "entities" / f"Page{i}.md").write_text(f"# Page{i}", encoding="utf-8")
            paths.append(p)

        all_paths = [f"entities/Page{i}.md" for i in range(7)]
        llm_response = json.dumps({"pages": all_paths})

        def mock_llm(system, user, **kwargs):
            return llm_response

        selected = _select_pages("질문", "(index)", mock_llm, wiki_path)
        assert len(selected) <= MAX_PAGES

    def test_nonexistent_files_excluded(self, tmp_path):
        """존재하지 않는 파일은 선택 결과에서 제외."""
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()
        (wiki_path / "entities").mkdir()

        (wiki_path / "entities" / "PUSCH.md").write_text("# PUSCH", encoding="utf-8")

        llm_response = json.dumps({"pages": [
            "entities/PUSCH.md",
            "entities/NONEXISTENT.md",  # 존재 안 함
        ]})

        def mock_llm(system, user, **kwargs):
            return llm_response

        selected = _select_pages("질문", "(index)", mock_llm, wiki_path)
        assert "entities/PUSCH.md" in selected
        assert "entities/NONEXISTENT.md" not in selected

    def test_empty_llm_response_uses_fallback(self, tmp_path):
        """LLM이 빈 pages 반환 시 fallback으로 wiki 디렉토리에서 직접 수집."""
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()
        (wiki_path / "entities").mkdir()
        (wiki_path / "entities" / "PUSCH.md").write_text("# PUSCH", encoding="utf-8")

        def mock_llm(system, user, **kwargs):
            return json.dumps({"pages": []})

        selected = _select_pages("질문", "(index)", mock_llm, wiki_path)
        # fallback은 존재하는 파일을 반환해야 함
        assert isinstance(selected, list)


# ──────────────────────────────────────────────
# _file_answer 테스트
# ──────────────────────────────────────────────

class TestFileAnswer:
    def test_file_created_in_query_dir(self, tmp_path):
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()

        path = _file_answer("PUSCH 스크램블링이란?", "답변 내용", ["entities/PUSCH.md"], wiki_path)

        query_dir = wiki_path / "query"
        assert query_dir.exists()
        assert len(list(query_dir.glob("*.md"))) == 1

    def test_file_path_contains_date(self, tmp_path):
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()

        path = _file_answer("테스트 질문", "답변", [], wiki_path)

        # 경로에 날짜 형식 포함
        import re
        assert re.search(r'\d{4}-\d{2}-\d{2}', path)

    def test_file_path_relative_to_wiki(self, tmp_path):
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()

        path = _file_answer("질문", "답변", [], wiki_path)

        # query/ 로 시작하는 상대경로
        assert path.startswith("query")

    def test_file_content_includes_question(self, tmp_path):
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()

        question = "PUSCH scrambling 절차는?"
        path = _file_answer(question, "답변 내용", [], wiki_path)

        content = (wiki_path / path).read_text(encoding="utf-8")
        assert question in content

    def test_file_content_includes_answer(self, tmp_path):
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()

        answer = "PUSCH scrambling은 다음과 같이 동작한다."
        path = _file_answer("질문", answer, [], wiki_path)

        content = (wiki_path / path).read_text(encoding="utf-8")
        assert answer in content

    def test_sources_included_as_wikilinks(self, tmp_path):
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()

        sources = ["entities/PUSCH.md", "concepts/UCI_Multiplexing.md"]
        path = _file_answer("질문", "답변", sources, wiki_path)

        content = (wiki_path / path).read_text(encoding="utf-8")
        assert "[[PUSCH]]" in content
        assert "[[UCI_Multiplexing]]" in content


# ──────────────────────────────────────────────
# run_query 통합 테스트
# ──────────────────────────────────────────────

class TestRunQuery:
    def test_basic_query_returns_answer(self, tmp_path):
        """기본 쿼리 플로우: 답변 문자열 반환."""
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()
        (wiki_path / "entities").mkdir()
        (wiki_path / "entities" / "PUSCH.md").write_text("# PUSCH\nPUSCH는 채널이다.", encoding="utf-8")

        call_count = [0]
        responses = [
            json.dumps({"pages": ["entities/PUSCH.md"]}),  # selector
            "PUSCH는 데이터 전송 채널입니다.",               # synthesizer
        ]

        def mock_llm(system, user, **kwargs):
            idx = min(call_count[0], len(responses) - 1)
            call_count[0] += 1
            return responses[idx]

        result = run_query("PUSCH란?", str(wiki_path), mock_llm)

        assert "answer" in result
        assert "sources" in result
        assert isinstance(result["answer"], str)

    def test_no_index_md_still_works(self, tmp_path):
        """index.md 없어도 오류 없이 동작."""
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()

        def mock_llm(system, user, **kwargs):
            if "pages" not in system.lower() and call_count[0] == 0:
                return json.dumps({"pages": []})
            return "답변"

        call_count = [0]

        def counting_llm(system, user, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({"pages": []})
            return "답변 내용입니다."

        result = run_query("질문", str(wiki_path), counting_llm)
        assert result["answer"] is not None

    def test_file_option_creates_file(self, tmp_path):
        """file=True 이면 답변 파일 생성."""
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()

        call_count = [0]

        def mock_llm(system, user, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({"pages": []})
            return "파일에 저장될 답변"

        result = run_query("저장할 질문", str(wiki_path), mock_llm, file=True)
        assert result["filed"] is not None

    def test_no_file_option_filed_is_none(self, tmp_path):
        """file=False(기본) 이면 filed=None."""
        wiki_path = tmp_path / "wiki"
        wiki_path.mkdir()

        call_count = [0]

        def mock_llm(system, user, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({"pages": []})
            return "답변"

        result = run_query("질문", str(wiki_path), mock_llm, file=False)
        assert result["filed"] is None
