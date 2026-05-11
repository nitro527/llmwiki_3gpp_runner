"""
test_post_plan.py — post_plan.py 단위 테스트

테스트 대상:
- _check_duplicate_sections(): 동일 파일+섹션이 여러 페이지에 있을 때 감지
- run_post_plan(): post_plan_done=True면 스킵
- run_post_plan(): post_plan_done 플래그 업데이트 및 저장
- _remove_empty_pages(): 소스 없는 페이지 제거
- _apply_fixes(): remove_source, remove_sections 액션 적용
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


from wiki_builder.post_plan import (
    _check_duplicate_sections,
    _remove_empty_pages,
    _apply_fixes,
    run_post_plan,
)


# ──────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────

_SENTINEL = object()

def _make_page(path, sections=_SENTINEL, file="sources/38211.docx"):
    """sections=None이면 기본값 ['6.3.1'] 사용. sections=[]이면 빈 목록 사용."""
    secs = ["6.3.1"] if sections is _SENTINEL else sections
    return {
        "path": path,
        "description": f"{path} 설명",
        "generated": False,
        "linked": False,
        "sources": [{"file": file, "sections": secs}],
    }


# ──────────────────────────────────────────────
# _check_duplicate_sections 테스트
# ──────────────────────────────────────────────

class TestCheckDuplicateSections:
    def test_no_duplicates_returns_empty(self):
        pages = [
            _make_page("entities/PUSCH.md", sections=["6.3.1"]),
            _make_page("entities/PDSCH.md", sections=["7.3.1"]),
        ]
        issues = _check_duplicate_sections(pages)
        assert issues == []

    def test_same_section_two_pages_detected(self):
        """동일 파일+섹션이 두 페이지에 있으면 감지."""
        pages = [
            _make_page("entities/PUSCH.md", sections=["6.3.1"]),
            _make_page("concepts/UCI_Multiplexing.md", sections=["6.3.1"]),  # 같은 섹션
        ]
        issues = _check_duplicate_sections(pages)
        assert len(issues) >= 1
        assert issues[0]["section"] == "6.3.1"
        assert "entities/PUSCH.md" in issues[0]["pages"]
        assert "concepts/UCI_Multiplexing.md" in issues[0]["pages"]

    def test_different_files_same_section_not_duplicate(self):
        """다른 파일의 같은 섹션 번호는 중복 아님."""
        page = {
            "path": "entities/PUSCH.md",
            "description": "PUSCH",
            "generated": False,
            "linked": False,
            "sources": [
                {"file": "sources/38211.docx", "sections": ["6.3.1"]},
                {"file": "sources/38212.docx", "sections": ["6.3.1"]},  # 다른 파일
            ],
        }
        issues = _check_duplicate_sections([page])
        assert issues == []

    def test_three_pages_same_section_detected(self):
        """세 페이지가 같은 섹션 → 하나의 이슈에 세 페이지 목록."""
        pages = [
            _make_page("entities/A.md", sections=["6.3.1"]),
            _make_page("entities/B.md", sections=["6.3.1"]),
            _make_page("entities/C.md", sections=["6.3.1"]),
        ]
        issues = _check_duplicate_sections(pages)
        assert len(issues) == 1
        assert len(issues[0]["pages"]) == 3

    def test_empty_pages_returns_empty(self):
        assert _check_duplicate_sections([]) == []

    def test_page_with_no_sections_ignored(self):
        """섹션이 없는 페이지는 중복 감지에서 제외된다."""
        pages = [
            _make_page("entities/PUSCH.md", sections=[]),  # 빈 섹션 → 중복 대상 없음
            _make_page("entities/PDSCH.md", sections=[]),
        ]
        issues = _check_duplicate_sections(pages)
        # 빈 섹션 목록은 key가 없으므로 중복 이슈 없음
        assert issues == []


# ──────────────────────────────────────────────
# _remove_empty_pages 테스트
# ──────────────────────────────────────────────

class TestRemoveEmptyPages:
    def test_page_with_sections_kept(self):
        pages = [_make_page("entities/PUSCH.md", sections=["6.3.1"])]
        removed = _remove_empty_pages(pages)
        assert removed == []
        assert len(pages) == 1

    def test_page_with_empty_sections_removed(self):
        pages = [
            _make_page("entities/PUSCH.md", sections=["6.3.1"]),
            _make_page("entities/EMPTY.md", sections=[]),
        ]
        removed = _remove_empty_pages(pages)
        assert "entities/EMPTY.md" in removed
        assert len(pages) == 1

    def test_page_with_no_sources_removed(self):
        pages = [{
            "path": "entities/NOSOURCE.md",
            "description": "소스 없음",
            "generated": False,
            "linked": False,
            "sources": [],
        }]
        removed = _remove_empty_pages(pages)
        assert "entities/NOSOURCE.md" in removed

    def test_multiple_empty_pages_all_removed(self):
        pages = [
            _make_page("entities/A.md", sections=[]),
            _make_page("entities/B.md", sections=[]),
            _make_page("entities/C.md", sections=["6.1"]),
        ]
        removed = _remove_empty_pages(pages)
        assert len(removed) == 2
        assert len(pages) == 1


# ──────────────────────────────────────────────
# _apply_fixes 테스트
# ──────────────────────────────────────────────

class TestApplyFixes:
    def test_remove_source_action(self):
        """remove_source 액션: 해당 파일의 소스 항목 제거."""
        pages = [{
            "path": "entities/PUSCH.md",
            "description": "PUSCH",
            "generated": False,
            "linked": False,
            "sources": [
                {"file": "sources/38211.docx", "sections": ["6.3.1"]},
                {"file": "sources/38212.docx", "sections": ["6.2.1"]},
            ],
        }]
        fixes = [{"path": "entities/PUSCH.md", "action": "remove_source", "file": "sources/38212.docx"}]
        _apply_fixes(pages, fixes)

        assert len(pages[0]["sources"]) == 1
        assert pages[0]["sources"][0]["file"] == "sources/38211.docx"

    def test_remove_sections_action(self):
        """remove_sections 액션: 특정 섹션만 제거."""
        pages = [{
            "path": "entities/PUSCH.md",
            "description": "PUSCH",
            "generated": False,
            "linked": False,
            "sources": [
                {"file": "sources/38211.docx", "sections": ["6.3.1", "6.3.2", "6.3.3"]},
            ],
        }]
        fixes = [{
            "path": "entities/PUSCH.md",
            "action": "remove_sections",
            "file": "sources/38211.docx",
            "sections_to_remove": ["6.3.2"],
        }]
        _apply_fixes(pages, fixes)

        src = pages[0]["sources"][0]
        assert "6.3.2" not in src["sections"]
        assert "6.3.1" in src["sections"]
        assert "6.3.3" in src["sections"]

    def test_ok_action_no_change(self):
        """ok 액션: 변경 없음."""
        pages = [_make_page("entities/PUSCH.md", sections=["6.3.1"])]
        fixes = [{"path": "entities/PUSCH.md", "action": "ok", "file": "sources/38211.docx"}]
        before_sources = list(pages[0]["sources"])
        _apply_fixes(pages, fixes)
        assert pages[0]["sources"] == before_sources

    def test_nonexistent_path_ignored(self):
        """존재하지 않는 path → 오류 없이 스킵."""
        pages = [_make_page("entities/PUSCH.md")]
        fixes = [{"path": "entities/NONEXISTENT.md", "action": "remove_source",
                   "file": "sources/38211.docx"}]
        _apply_fixes(pages, fixes)  # 오류 없이 실행

    def test_all_sections_removed_source_deleted(self):
        """모든 섹션이 remove_sections로 제거되면 소스 항목 전체 삭제."""
        pages = [{
            "path": "entities/PUSCH.md",
            "description": "PUSCH",
            "generated": False,
            "linked": False,
            "sources": [
                {"file": "sources/38211.docx", "sections": ["6.3.1"]},
            ],
        }]
        fixes = [{
            "path": "entities/PUSCH.md",
            "action": "remove_sections",
            "file": "sources/38211.docx",
            "sections_to_remove": ["6.3.1"],
        }]
        _apply_fixes(pages, fixes)
        assert len(pages[0]["sources"]) == 0


# ──────────────────────────────────────────────
# run_post_plan 통합 테스트
# ──────────────────────────────────────────────

class TestRunPostPlan:
    def test_already_done_skipped(self, tmp_path):
        """post_plan_done=True이면 LLM 호출 없이 그대로 반환."""
        plan_path = tmp_path / "plan.json"
        plan = {
            "post_plan_done": True,
            "planned_sources": [],
            "pages": [_make_page("entities/PUSCH.md")],
        }

        call_count = [0]

        def mock_llm(system, user, **kwargs):
            call_count[0] += 1
            return "[]"

        result = run_post_plan(plan, str(plan_path), mock_llm)
        assert call_count[0] == 0
        assert result["post_plan_done"] is True

    def test_post_plan_done_flag_set(self, tmp_path):
        """run_post_plan 완료 후 plan.json에 post_plan_done=True 저장."""
        plan_path = tmp_path / "plan.json"
        plan = {
            "post_plan_done": False,
            "planned_sources": [],
            "pages": [_make_page("entities/PUSCH.md")],
        }

        def mock_llm(system, user, **kwargs):
            return "[]"

        result = run_post_plan(plan, str(plan_path), mock_llm)

        assert result["post_plan_done"] is True
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        assert saved["post_plan_done"] is True

    def test_duplicate_section_detected_in_result(self, tmp_path):
        """중복 섹션 있을 때 오류 없이 완료."""
        plan_path = tmp_path / "plan.json"
        plan = {
            "post_plan_done": False,
            "planned_sources": [],
            "pages": [
                _make_page("entities/PUSCH.md", sections=["6.3.1"]),
                _make_page("concepts/UCI_Multiplexing.md", sections=["6.3.1"]),
            ],
        }

        def mock_llm(system, user, **kwargs):
            return "[]"

        result = run_post_plan(plan, str(plan_path), mock_llm)
        # 중복 섹션이 있어도 완료됨
        assert result["post_plan_done"] is True

    def test_llm_fixes_applied(self, tmp_path):
        """LLM이 remove_source fix를 반환하면 적용된다."""
        plan_path = tmp_path / "plan.json"
        page = {
            "path": "entities/PUSCH.md",
            "description": "PUSCH",
            "generated": False,
            "linked": False,
            "sources": [
                {"file": "sources/38211.docx", "sections": ["6.3.1"]},
                {"file": "sources/38212.docx", "sections": ["6.2.1"]},
            ],
        }
        plan = {
            "post_plan_done": False,
            "planned_sources": [],
            "pages": [page],
        }

        fix_response = json.dumps([{
            "path": "entities/PUSCH.md",
            "action": "remove_source",
            "file": "sources/38212.docx",
        }])

        def mock_llm(system, user, **kwargs):
            return fix_response

        result = run_post_plan(plan, str(plan_path), mock_llm)

        pusch = next(p for p in result["pages"] if p["path"] == "entities/PUSCH.md")
        source_files = [s["file"] for s in pusch["sources"]]
        assert "sources/38212.docx" not in source_files

    def test_empty_pages_plan_still_completes(self, tmp_path):
        """pages가 빈 plan도 오류 없이 완료."""
        plan_path = tmp_path / "plan.json"
        plan = {"post_plan_done": False, "planned_sources": [], "pages": []}

        def mock_llm(system, user, **kwargs):
            return "[]"

        result = run_post_plan(plan, str(plan_path), mock_llm)
        assert result["post_plan_done"] is True
