"""
test_post_lint.py — run_post_lint 단위 테스트

테스트 대상:
  - broken_links 있을 때 plan에 페이지 추가되는지
  - missing_backlinks 있을 때 linked 플래그 리셋되는지
  - contradictions 있을 때 generated 플래그 리셋되는지
  - 빈 이슈일 때 아무것도 변경되지 않는지
  - _infer_path: 대문자 약어 → entities/, 그 외 → concepts/
  - _collect_broken_candidates: 중복 제거, 기존 plan 경로 제외

LLM mock: 실제 API 호출 없음. 사용자 입력은 EOFError mock으로 대체.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


from wiki_builder.lint import (
    run_post_lint,
    _infer_path,
    _collect_broken_candidates,
)


# ──────────────────────────────────────────────
# 픽스처
# ──────────────────────────────────────────────

def _make_plan(pages: list[dict]) -> dict:
    """테스트용 최소 plan dict."""
    return {"planned_sources": [], "pages": pages}


def _base_page(path: str, generated: bool = True, linked: bool = True) -> dict:
    return {
        "path": path,
        "description": "테스트 페이지",
        "generated": generated,
        "linked": linked,
        "sources": [],
    }


# ──────────────────────────────────────────────
# 1. broken_links → plan에 신규 페이지 추가
# ──────────────────────────────────────────────

class TestBrokenLinksAddPage:
    """broken_links 이슈가 있을 때 사용자가 y로 응답하면 plan에 페이지가 추가된다."""

    def test_new_page_added_to_plan_on_yes(self, tmp_path):
        """broken link 'PDSCH'(대문자) → _infer_path에 따라 entities/PDSCH.md로 추가."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([_base_page("entities/PUSCH.md")])

        report = {
            "broken_links": [
                {"page": "entities/PUSCH.md", "link": "PDSCH"}
            ],
            "missing_backlinks": [],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="y"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        # "PDSCH" → 대문자 약어 → entities/PDSCH.md
        assert "entities/PDSCH.md" in result["added_pages"]
        assert result["needs_generate"] is True
        assert result["needs_link"] is True

        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        paths = [p["path"] for p in saved["pages"]]
        assert "entities/PDSCH.md" in paths

    def test_uppercase_link_goes_to_entities(self, tmp_path):
        """대문자 약어([[HARQ]]) → entities/HARQ.md 로 추가."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([_base_page("entities/PUSCH.md")])

        report = {
            "broken_links": [
                {"page": "entities/PUSCH.md", "link": "HARQ"}
            ],
            "missing_backlinks": [],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="y"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert "entities/HARQ.md" in result["added_pages"]

    def test_no_page_added_on_no(self, tmp_path):
        """사용자가 n으로 응답하면 plan에 추가 없음."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([_base_page("entities/PUSCH.md")])

        report = {
            "broken_links": [
                {"page": "entities/PUSCH.md", "link": "SomeNewPage"}  # 소문자 → concepts/
            ],
            "missing_backlinks": [],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="n"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert result["added_pages"] == []
        assert result["needs_generate"] is False
        # plan 파일 미저장 확인
        assert not plan_path.exists()

    def test_already_existing_link_not_duplicated(self, tmp_path):
        """이미 plan에 있는 경로는 중복 추가하지 않는다.
        'PDSCH'(대문자) → _infer_path → entities/PDSCH.md → 이미 plan에 있으므로 스킵."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([
            _base_page("entities/PUSCH.md"),
            _base_page("entities/PDSCH.md"),  # 이미 존재
        ])

        report = {
            "broken_links": [
                {"page": "entities/PUSCH.md", "link": "PDSCH"}
            ],
            "missing_backlinks": [],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="y"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        # candidates가 없으므로 input도 호출 안 됨 → added_pages 비어야 함
        assert result["added_pages"] == []

    def test_eofenror_treated_as_no(self, tmp_path):
        """비대화형 환경(EOFError) → n으로 처리."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([_base_page("entities/PUSCH.md")])

        report = {
            "broken_links": [
                {"page": "entities/PUSCH.md", "link": "SomeNewPage"}  # 소문자 → concepts/ (새 경로)
            ],
            "missing_backlinks": [],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", side_effect=EOFError):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert result["added_pages"] == []

    def test_keyboard_interrupt_treated_as_no(self, tmp_path):
        """KeyboardInterrupt → n으로 처리 — 추가 없음."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([_base_page("entities/PUSCH.md")])

        report = {
            "broken_links": [
                {"page": "entities/PUSCH.md", "link": "NEWPAGE"}
            ],
            "missing_backlinks": [],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", side_effect=KeyboardInterrupt):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert result["added_pages"] == []
        assert result["needs_generate"] is False

    def test_all_broken_already_in_plan_no_prompt(self, tmp_path):
        """broken_links가 있어도 모두 plan에 이미 있으면 input 호출 없이 스킵."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([
            _base_page("entities/PUSCH.md"),
            _base_page("entities/PDSCH.md"),  # 이미 존재
        ])

        report = {
            "broken_links": [
                {"page": "entities/PUSCH.md", "link": "PDSCH"}
            ],
            "missing_backlinks": [],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        # input이 절대 호출되면 안 됨 (호출 시 예외 발생시켜 확인)
        with patch("builtins.input", side_effect=AssertionError("input이 호출됨")):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert result["added_pages"] == []
        assert result["needs_generate"] is False

    def test_added_page_has_correct_description_and_flags(self, tmp_path):
        """plan에 추가된 신규 페이지가 generated=False, linked=False, description 포함 여부."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([_base_page("entities/PUSCH.md")])

        report = {
            "broken_links": [
                {"page": "entities/PUSCH.md", "link": "HARQ"}
            ],
            "missing_backlinks": [],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="y"):
            run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        new_page = next((p for p in saved["pages"] if p["path"] == "entities/HARQ.md"), None)
        assert new_page is not None
        assert new_page["generated"] is False
        assert new_page["linked"] is False
        assert isinstance(new_page["description"], str) and len(new_page["description"]) > 0


# ──────────────────────────────────────────────
# 2. missing_backlinks → linked 플래그 리셋
# ──────────────────────────────────────────────

class TestMissingBacklinksResetLinked:
    """missing_backlinks 이슈 → y 응답 시 해당 페이지의 linked 플래그가 False로 리셋."""

    def test_linked_flag_reset_on_yes(self, tmp_path):
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([
            _base_page("entities/PUSCH.md", linked=True),
            _base_page("concepts/UCI.md", linked=True),
        ])

        report = {
            "broken_links": [],
            "missing_backlinks": [
                {"page": "entities/PUSCH.md", "missing_from": "concepts/UCI.md"}
            ],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="y"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert "entities/PUSCH.md" in result["relink_pages"]
        assert result["needs_link"] is True

        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        pusch = next(p for p in saved["pages"] if p["path"] == "entities/PUSCH.md")
        assert pusch["linked"] is False

    def test_unrelated_page_linked_unchanged(self, tmp_path):
        """역링크 이슈 없는 페이지의 linked는 변경 없음."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([
            _base_page("entities/PUSCH.md", linked=True),
            _base_page("concepts/UCI.md", linked=True),
        ])

        report = {
            "broken_links": [],
            "missing_backlinks": [
                {"page": "entities/PUSCH.md", "missing_from": "concepts/UCI.md"}
            ],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="y"):
            run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        uci = next(p for p in saved["pages"] if p["path"] == "concepts/UCI.md")
        assert uci["linked"] is True  # UCI는 relink 대상 아님

    def test_linked_not_reset_on_no(self, tmp_path):
        """n 응답 시 linked 플래그 변경 없음."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([_base_page("entities/PUSCH.md", linked=True)])

        report = {
            "broken_links": [],
            "missing_backlinks": [
                {"page": "entities/PUSCH.md", "missing_from": "concepts/UCI.md"}
            ],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="n"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert result["relink_pages"] == []
        assert result["needs_link"] is False

    def test_deduplication_of_relink_pages(self, tmp_path):
        """동일 페이지가 여러 missing_backlinks 항목에 있어도 한 번만 relink."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([
            _base_page("entities/PUSCH.md", linked=True),
        ])

        report = {
            "broken_links": [],
            "missing_backlinks": [
                {"page": "entities/PUSCH.md", "missing_from": "concepts/A.md"},
                {"page": "entities/PUSCH.md", "missing_from": "concepts/B.md"},
            ],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="y"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert result["relink_pages"].count("entities/PUSCH.md") == 1


# ──────────────────────────────────────────────
# 3. contradictions → generated 플래그 리셋
# ──────────────────────────────────────────────

class TestContradictionsResetGenerated:
    """contradictions 이슈 → y 응답 시 해당 페이지의 generated, linked 플래그가 False로 리셋."""

    def test_generated_and_linked_reset_on_yes(self, tmp_path):
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([
            _base_page("entities/PUSCH.md", generated=True, linked=True),
            _base_page("concepts/UCI.md", generated=True, linked=True),
        ])

        report = {
            "broken_links": [],
            "missing_backlinks": [],
            "contradictions": [
                {
                    "pages": ["entities/PUSCH.md", "concepts/UCI.md"],
                    "issue": "PUSCH 최대 전송 계층 수가 서로 다르게 기술됨"
                }
            ],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="y"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert "entities/PUSCH.md" in result["reset_pages"]
        assert "concepts/UCI.md" in result["reset_pages"]
        assert result["needs_generate"] is True
        assert result["needs_link"] is True

        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        for p in saved["pages"]:
            assert p["generated"] is False
            assert p["linked"] is False

    def test_generated_not_reset_on_no(self, tmp_path):
        """n 응답 시 generated/linked 변경 없음."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([_base_page("entities/PUSCH.md", generated=True, linked=True)])

        report = {
            "broken_links": [],
            "missing_backlinks": [],
            "contradictions": [
                {"pages": ["entities/PUSCH.md"], "issue": "테스트 모순"}
            ],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="n"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert result["reset_pages"] == []
        assert result["needs_generate"] is False

    def test_page_not_in_plan_not_added(self, tmp_path):
        """모순 페이지가 plan에 없어도 오류 없이 처리."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([_base_page("entities/PUSCH.md")])

        report = {
            "broken_links": [],
            "missing_backlinks": [],
            "contradictions": [
                {"pages": ["entities/NONEXISTENT.md"], "issue": "테스트"}
            ],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="y"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        # plan에 없는 페이지 → reset_pages에 포함되지 않음
        assert "entities/NONEXISTENT.md" not in result["reset_pages"]


# ──────────────────────────────────────────────
# 4. 빈 이슈 — 아무것도 변경 없음
# ──────────────────────────────────────────────

class TestEmptyReport:
    def test_empty_report_no_changes(self, tmp_path):
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([_base_page("entities/PUSCH.md")])

        report = {
            "broken_links": [],
            "missing_backlinks": [],
            "contradictions": [],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert result["added_pages"] == []
        assert result["reset_pages"] == []
        assert result["relink_pages"] == []
        assert result["needs_generate"] is False
        assert result["needs_link"] is False
        # 변경 없으므로 plan 파일 저장 안 됨
        assert not plan_path.exists()


# ──────────────────────────────────────────────
# 5. _infer_path 단위 테스트
# ──────────────────────────────────────────────

class TestInferPath:
    def test_uppercase_acronym_to_entities(self):
        assert _infer_path("PUSCH", set()) == "entities/PUSCH.md"

    def test_uppercase_two_char_to_entities(self):
        assert _infer_path("NR", set()) == "entities/NR.md"

    def test_mixed_case_to_concepts(self):
        assert _infer_path("Scrambling", set()) == "concepts/Scrambling.md"

    def test_lowercase_to_concepts(self):
        assert _infer_path("rate_matching", set()) == "concepts/rate_matching.md"

    def test_single_uppercase_char_to_concepts(self):
        """단일 대문자는 약어로 보지 않음 → concepts/."""
        assert _infer_path("A", set()) == "concepts/A.md"

    def test_uppercase_with_underscore_to_entities(self):
        """대문자+밑줄: clean 후 대문자만 남으면 entities/."""
        # "HARQ_ACK" → clean = "HARQACK" (대문자만) → entities/
        assert _infer_path("HARQ_ACK", set()) == "entities/HARQ_ACK.md"


# ──────────────────────────────────────────────
# 6. _collect_broken_candidates 단위 테스트
# ──────────────────────────────────────────────

class TestCollectBrokenCandidates:
    def test_new_link_becomes_candidate(self):
        """'PDSCH'(대문자) → _infer_path → entities/PDSCH.md 후보."""
        broken = [{"page": "entities/A.md", "link": "PDSCH"}]
        existing = {"entities/A.md"}
        candidates = _collect_broken_candidates(broken, existing)
        assert len(candidates) == 1
        assert candidates[0]["path"] == "entities/PDSCH.md"

    def test_new_lowercase_link_becomes_concept_candidate(self):
        """소문자 링크 → concepts/ 후보."""
        broken = [{"page": "entities/A.md", "link": "scrambling"}]
        existing = {"entities/A.md"}
        candidates = _collect_broken_candidates(broken, existing)
        assert len(candidates) == 1
        assert candidates[0]["path"] == "concepts/scrambling.md"

    def test_already_existing_path_excluded(self):
        """이미 plan에 있는 경로는 후보에서 제외 (대문자 → entities/ 경로로 확인)."""
        broken = [{"page": "entities/A.md", "link": "PDSCH"}]
        existing = {"entities/A.md", "entities/PDSCH.md"}  # entities/PDSCH.md 이미 존재
        candidates = _collect_broken_candidates(broken, existing)
        assert candidates == []

    def test_duplicate_links_deduped(self):
        broken = [
            {"page": "entities/A.md", "link": "NewPage"},
            {"page": "entities/B.md", "link": "NewPage"},
        ]
        existing = set()
        candidates = _collect_broken_candidates(broken, existing)
        paths = [c["path"] for c in candidates]
        assert paths.count("concepts/NewPage.md") == 1

    def test_from_page_recorded(self):
        broken = [{"page": "entities/A.md", "link": "PDSCH"}]
        existing = set()
        candidates = _collect_broken_candidates(broken, existing)
        assert candidates[0]["from_page"] == "entities/A.md"


# ──────────────────────────────────────────────
# 7. 복합 시나리오 — 여러 이슈 동시 처리
# ──────────────────────────────────────────────

class TestCombinedScenario:
    def test_broken_and_contradiction_both_handled(self, tmp_path):
        """broken_links와 contradictions가 동시에 있을 때 각각 올바르게 처리."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([
            _base_page("entities/PUSCH.md", generated=True, linked=True),
        ])

        report = {
            "broken_links": [
                {"page": "entities/PUSCH.md", "link": "PDSCH"}
            ],
            "missing_backlinks": [],
            "contradictions": [
                {"pages": ["entities/PUSCH.md"], "issue": "모순"}
            ],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        # 두 질문 모두 y
        with patch("builtins.input", return_value="y"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert result["needs_generate"] is True
        assert result["needs_link"] is True
        assert len(result["added_pages"]) >= 1
        assert len(result["reset_pages"]) >= 1

    def test_needs_flags_false_when_all_skipped(self, tmp_path):
        """모든 질문에 n으로 답하면 needs_* 플래그 모두 False."""
        plan_path = tmp_path / "plan.json"
        plan = _make_plan([
            _base_page("entities/PUSCH.md", generated=True, linked=True),
        ])

        report = {
            "broken_links": [{"page": "entities/PUSCH.md", "link": "PDSCH"}],
            "missing_backlinks": [
                {"page": "entities/PUSCH.md", "missing_from": "concepts/UCI.md"}
            ],
            "contradictions": [
                {"pages": ["entities/PUSCH.md"], "issue": "모순"}
            ],
            "orphan_pages": [],
            "stale_claims": [],
            "data_gaps": [],
        }

        with patch("builtins.input", return_value="n"):
            result = run_post_lint(report=report, plan=plan, plan_path=str(plan_path))

        assert result["needs_generate"] is False
        assert result["needs_link"] is False
