"""
test_plan.py — plan.py 단위 테스트

테스트 대상:
- _parse_planner_response(): JSON 파싱, path 유효성, concepts/ 동작어 검사
- run_plan(): 증분 처리 (planned_sources에 있는 소스 스킵)
- run_plan(): 동일 path 멀티소스 머지 (sources 배열 합산)
- run_plan(): description 업데이트 (더 넓은 description으로 교체)
- _save_plan_incremental(): plan.json 저장 형식 검증
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


from wiki_builder.plan import (
    _parse_planner_response,
    _save_plan_incremental,
    _collect_sources,
    run_plan,
)


# ──────────────────────────────────────────────
# _parse_planner_response 테스트
# ──────────────────────────────────────────────

class TestParsePlannerResponse:
    def _src(self, file="sources/38211.docx"):
        return file

    def test_valid_json_array_parsed(self):
        raw = json.dumps([{
            "path": "entities/PUSCH.md",
            "description": "PUSCH 채널 설명",
            "sections": ["6.3.1"]
        }])
        result = _parse_planner_response(raw, "sources/38211.docx")
        assert result is not None
        assert len(result) == 1
        assert result[0]["path"] == "entities/PUSCH.md"

    def test_valid_pages_dict_parsed(self):
        raw = json.dumps({"pages": [{
            "path": "concepts/UCI_Multiplexing.md",
            "description": "UCI 멀티플렉싱",
            "sections": ["6.3.2"]
        }]})
        result = _parse_planner_response(raw, "sources/38212.docx")
        assert result is not None
        assert result[0]["path"] == "concepts/UCI_Multiplexing.md"

    def test_code_block_stripped(self):
        raw = '```json\n[{"path": "entities/PUSCH.md", "description": "desc", "sections": []}]\n```'
        result = _parse_planner_response(raw, "sources/38211.docx")
        assert result is not None
        assert len(result) == 1

    def test_invalid_path_prefix_rejected(self):
        raw = json.dumps([{
            "path": "invalid/PUSCH.md",
            "description": "설명",
            "sections": []
        }])
        result = _parse_planner_response(raw, "sources/38211.docx")
        assert result == []

    def test_path_without_md_rejected(self):
        raw = json.dumps([{
            "path": "entities/PUSCH",
            "description": "설명",
            "sections": []
        }])
        result = _parse_planner_response(raw, "sources/38211.docx")
        assert result == []

    def test_concepts_without_underscore_rejected(self):
        """concepts/ 경로는 동작어(_포함) 필수."""
        raw = json.dumps([{
            "path": "concepts/UCI.md",
            "description": "UCI 설명",
            "sections": []
        }])
        result = _parse_planner_response(raw, "sources/38212.docx")
        assert result == []

    def test_concepts_with_underscore_accepted(self):
        raw = json.dumps([{
            "path": "concepts/UCI_Multiplexing.md",
            "description": "UCI 멀티플렉싱 설명",
            "sections": ["6.3.2"]
        }])
        result = _parse_planner_response(raw, "sources/38212.docx")
        assert result is not None and len(result) == 1

    def test_missing_path_skipped(self):
        raw = json.dumps([{
            "description": "설명만 있음",
            "sections": []
        }])
        result = _parse_planner_response(raw, "sources/38211.docx")
        assert result == []

    def test_missing_description_skipped(self):
        raw = json.dumps([{
            "path": "entities/PUSCH.md",
            "sections": []
        }])
        result = _parse_planner_response(raw, "sources/38211.docx")
        assert result == []

    def test_unparseable_json_returns_none(self):
        result = _parse_planner_response("이것은 JSON이 아닙니다", "sources/38211.docx")
        assert result is None

    def test_generated_and_linked_flags_set_false(self):
        raw = json.dumps([{
            "path": "entities/PUSCH.md",
            "description": "PUSCH",
            "sections": ["6.3.1"]
        }])
        result = _parse_planner_response(raw, "sources/38211.docx")
        assert result[0]["generated"] is False
        assert result[0]["linked"] is False

    def test_source_file_recorded(self):
        raw = json.dumps([{
            "path": "entities/PUSCH.md",
            "description": "PUSCH",
            "sections": ["6.3.1"]
        }])
        result = _parse_planner_response(raw, "sources/38211.docx")
        assert result[0]["sources"][0]["file"] == "sources/38211.docx"
        assert "6.3.1" in result[0]["sources"][0]["sections"]


# ──────────────────────────────────────────────
# _save_plan_incremental 테스트
# ──────────────────────────────────────────────

class TestSavePlanIncremental:
    def test_saves_json_file(self, tmp_path):
        plan_path = tmp_path / "plan.json"
        pages = [{"path": "entities/PUSCH.md", "description": "PUSCH", "generated": False,
                   "linked": False, "sources": []}]
        _save_plan_incremental(str(plan_path), {"sources/38211.docx"}, pages)
        assert plan_path.exists()

    def test_saved_json_is_valid(self, tmp_path):
        plan_path = tmp_path / "plan.json"
        pages = [{"path": "entities/PUSCH.md", "description": "PUSCH", "generated": False,
                   "linked": False, "sources": []}]
        _save_plan_incremental(str(plan_path), {"sources/38211.docx"}, pages)
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        assert "pages" in data
        assert "planned_sources" in data

    def test_planned_sources_sorted(self, tmp_path):
        plan_path = tmp_path / "plan.json"
        _save_plan_incremental(str(plan_path), {"b.docx", "a.docx", "c.docx"}, [])
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        assert data["planned_sources"] == sorted(["a.docx", "b.docx", "c.docx"])

    def test_post_plan_done_false(self, tmp_path):
        plan_path = tmp_path / "plan.json"
        _save_plan_incremental(str(plan_path), set(), [])
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        assert data["post_plan_done"] is False

    def test_returns_plan_dict(self, tmp_path):
        plan_path = tmp_path / "plan.json"
        result = _save_plan_incremental(str(plan_path), set(), [])
        assert isinstance(result, dict)
        assert "pages" in result


# ──────────────────────────────────────────────
# run_plan 증분 처리 테스트
# ──────────────────────────────────────────────

class TestRunPlanIncremental:
    """이미 planned_sources에 있는 소스는 다시 처리하지 않는다."""

    def test_already_planned_source_skipped(self, tmp_path):
        """plan.json에 이미 있는 소스 → 증분 없이 기존 plan 반환."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()

        # 소스 파일 생성
        src_file = sources_dir / "38211.txt"
        src_file.write_text("dummy content", encoding="utf-8")

        # 이미 해당 소스가 planned된 plan.json 생성
        plan_path = tmp_path / "plan.json"
        existing_plan = {
            "post_plan_done": False,
            "planned_sources": [str(src_file)],
            "pages": [{"path": "entities/PUSCH.md", "description": "PUSCH",
                       "generated": True, "linked": True, "sources": []}],
        }
        plan_path.write_text(json.dumps(existing_plan), encoding="utf-8")

        call_count = [0]

        def mock_llm(system, user, **kwargs):
            call_count[0] += 1
            return "[]"

        def mock_chunk(path, **kwargs):
            return [{"index": 0, "text": "chunk text", "start": 0, "end": 100}]

        result = run_plan(
            sources_dir=str(sources_dir),
            wiki_dir=str(wiki_dir),
            plan_path=str(plan_path),
            call_llm=mock_llm,
            chunk_fn=mock_chunk,
            backend="claude",
        )

        # LLM 호출 없이 기존 plan 반환
        assert call_count[0] == 0
        assert len(result["pages"]) == 1

    def test_new_source_triggers_planning(self, tmp_path):
        """새로운 소스 파일이 있으면 LLM 호출이 발생한다."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()

        src_file = sources_dir / "new_spec.txt"
        src_file.write_text("새 소스 내용", encoding="utf-8")

        plan_path = tmp_path / "plan.json"
        # 다른 소스만 planned된 plan
        existing_plan = {
            "post_plan_done": False,
            "planned_sources": ["other/file.docx"],
            "pages": [],
        }
        plan_path.write_text(json.dumps(existing_plan), encoding="utf-8")

        call_count = [0]

        def mock_llm(system, user, **kwargs):
            call_count[0] += 1
            return json.dumps([{
                "path": "entities/PUSCH.md",
                "description": "PUSCH 채널",
                "sections": ["6.3.1"]
            }])

        def mock_chunk(path, **kwargs):
            return [{"index": 0, "text": "chunk", "start": 0, "end": 100}]

        result = run_plan(
            sources_dir=str(sources_dir),
            wiki_dir=str(wiki_dir),
            plan_path=str(plan_path),
            call_llm=mock_llm,
            chunk_fn=mock_chunk,
        )

        assert call_count[0] > 0
        assert str(src_file) in result["planned_sources"]


class TestRunPlanMultiSourceMerge:
    """동일 path가 여러 소스에서 등장하면 sources 배열을 합산한다."""

    def test_same_path_from_two_sources_merged(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()

        src1 = sources_dir / "spec1.txt"
        src1.write_text("spec1", encoding="utf-8")
        src2 = sources_dir / "spec2.txt"
        src2.write_text("spec2", encoding="utf-8")

        plan_path = tmp_path / "plan.json"

        call_count = [0]
        responses = [
            json.dumps([{"path": "entities/PUSCH.md", "description": "PUSCH from spec1", "sections": ["6.1"]}]),
            json.dumps([{"path": "entities/PUSCH.md", "description": "PUSCH from spec1 and spec2 — broader", "sections": ["7.1"]}]),
        ]

        def mock_llm(system, user, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return responses[min(idx, len(responses) - 1)]

        def mock_chunk(path, **kwargs):
            return [{"index": 0, "text": "chunk", "start": 0, "end": 100}]

        result = run_plan(
            sources_dir=str(sources_dir),
            wiki_dir=str(wiki_dir),
            plan_path=str(plan_path),
            call_llm=mock_llm,
            chunk_fn=mock_chunk,
        )

        # PUSCH.md가 하나만 존재해야 함
        pusch_pages = [p for p in result["pages"] if p["path"] == "entities/PUSCH.md"]
        assert len(pusch_pages) == 1
        # 두 소스 파일이 모두 sources에 포함되어야 함
        source_files = [s["file"] for s in pusch_pages[0]["sources"]]
        assert len(source_files) == 2

    def test_description_updated_to_broader(self, tmp_path):
        """새 소스에서 더 긴 description이 나오면 교체된다."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()

        src1 = sources_dir / "spec1.txt"
        src1.write_text("spec1", encoding="utf-8")
        src2 = sources_dir / "spec2.txt"
        src2.write_text("spec2", encoding="utf-8")

        plan_path = tmp_path / "plan.json"

        call_count = [0]
        responses = [
            json.dumps([{"path": "entities/PUSCH.md", "description": "짧은 설명", "sections": ["6.1"]}]),
            json.dumps([{"path": "entities/PUSCH.md", "description": "더 길고 자세한 설명으로 교체되어야 한다", "sections": ["7.1"]}]),
        ]

        def mock_llm(system, user, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return responses[min(idx, len(responses) - 1)]

        def mock_chunk(path, **kwargs):
            return [{"index": 0, "text": "chunk", "start": 0, "end": 100}]

        result = run_plan(
            sources_dir=str(sources_dir),
            wiki_dir=str(wiki_dir),
            plan_path=str(plan_path),
            call_llm=mock_llm,
            chunk_fn=mock_chunk,
        )

        pusch = next(p for p in result["pages"] if p["path"] == "entities/PUSCH.md")
        assert len(pusch["description"]) > len("짧은 설명")


# ──────────────────────────────────────────────
# _parse_planner_response — 불완전 JSON 복구 (step 3)
# ──────────────────────────────────────────────

class TestParsePlannerResponseIncompleteJson:
    """max_tokens로 잘린 JSON 배열도 복구할 수 있어야 한다."""

    def test_truncated_json_array_recovered(self):
        """마지막 객체가 잘려도 완성된 객체들은 복구된다."""
        # 완전한 객체 2개 + 잘린 3번째 객체
        raw = (
            '[{"path": "entities/PUSCH.md", "description": "PUSCH 설명", "sections": ["6.1"]}, '
            '{"path": "entities/PDSCH.md", "description": "PDSCH 설명", "sections": ["7.1"]}, '
            '{"path": "entities/UCI.md", "description": "UCI 설'  # 잘림
        )
        result = _parse_planner_response(raw, "sources/38211.docx")
        # 완성된 객체 2개는 반환되어야 한다
        assert result is not None
        assert len(result) == 2
        paths = [p["path"] for p in result]
        assert "entities/PUSCH.md" in paths
        assert "entities/PDSCH.md" in paths

    def test_truncated_single_valid_object_recovered(self):
        """완성된 객체 1개 + 잘린 나머지 → 완성 객체 1개 반환."""
        raw = (
            '[{"path": "entities/PUSCH.md", "description": "PUSCH 채널", "sections": []}, '
            '{"path": "entities/PDSCH.md", "description": "PDSc'  # 잘림
        )
        result = _parse_planner_response(raw, "sources/38211.docx")
        assert result is not None
        assert len(result) == 1
        assert result[0]["path"] == "entities/PUSCH.md"

    def test_completely_broken_no_complete_object_returns_none(self):
        """완성된 객체가 하나도 없이 완전히 잘린 경우 None 반환."""
        raw = '[{"path": "entities/PUSCH.md", "desc'  # 첫 객체도 미완성
        result = _parse_planner_response(raw, "sources/38211.docx")
        # rfind('},') 가 -1이므로 복구 불가 → None
        assert result is None
