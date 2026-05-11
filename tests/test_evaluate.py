"""
test_evaluate.py — evaluate.py 단위 테스트

테스트 대상:
- _quick_check(): 필수 섹션 누락 시 점수 차감
- _quick_check(): bold 감지 (**text**) 시 이슈 추가
- _quick_check(): 관계타입 없는 링크 감지
- EvalHistory: new_session → add_round → close_session 흐름
- _compute_delta(): net_fixed, avg_score_delta 계산
- _apply_prompt_fix(): 전체 내용 기준 중복 패치 검사
- run_evaluate(): LLM 실패 시 continue로 다음 라운드 진행
- run_evaluate(): 파싱 실패 시 continue로 다음 라운드 진행
"""
import json
import sys
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest


from wiki_builder.evaluate import (
    _quick_check,
    _compute_delta,
    _apply_prompt_fix,
    EvalHistory,
    PASS_SCORE,
)


# ──────────────────────────────────────────────
# _quick_check 테스트
# ──────────────────────────────────────────────

FULL_CONTENT = """## 정의
PUSCH는 Physical Uplink Shared Channel이다.

## 요약
요약 내용

## 상세 설명
상세한 설명

## 인과 관계
인과 관계 설명

## 관련 개념
- [[UCI (uses)]]
- [[PUCCH (related)]]

## 스펙 근거
3GPP TS 38.211 Section 6.3.1

## 소스
sources/38211.docx
"""


class TestQuickCheck:
    def test_full_valid_content_passes(self):
        """모든 섹션 있고 bold 없고 관계타입 있으면 높은 점수."""
        result = _quick_check(FULL_CONTENT)
        assert result["pass"] is True
        assert result["score"] >= PASS_SCORE

    def test_missing_sections_lowers_score(self):
        """필수 섹션 3개 이상 누락 시 structure_score=0."""
        content = "## 정의\n내용만 있음"
        result = _quick_check(content)
        # 섹션 많이 누락 → structure_score = 0 → 전체 점수 낮아짐
        assert result["score"] < PASS_SCORE

    def test_missing_sections_adds_issue(self):
        """누락 섹션이 있으면 issues에 기록."""
        content = "## 정의\n내용"
        result = _quick_check(content)
        missing_issues = [i for i in result["issues"] if "누락 섹션" in i]
        assert len(missing_issues) >= 1

    def test_bold_text_detected(self):
        """**bold** 사용 시 이슈 추가."""
        content = FULL_CONTENT + "\n**중요한 내용**\n"
        result = _quick_check(content)
        bold_issues = [i for i in result["issues"] if "bold" in i or "**" in i]
        assert len(bold_issues) >= 1

    def test_bold_text_reduces_score(self):
        """**bold** 있으면 no_translation 점수 0."""
        content_with_bold = FULL_CONTENT + "\n**bold 텍스트**\n"
        result_bold = _quick_check(content_with_bold)
        result_clean = _quick_check(FULL_CONTENT)
        assert result_clean["score"] > result_bold["score"]

    def test_no_relation_type_in_links_detected(self):
        """관계타입 없는 [[링크]] 감지."""
        content = FULL_CONTENT.replace(
            "- [[UCI (uses)]]\n- [[PUCCH (related)]]",
            "- [[UCI]]\n- [[PUCCH]]"
        )
        result = _quick_check(content)
        relation_issues = [i for i in result["issues"] if "관계 타입" in i]
        assert len(relation_issues) >= 1

    def test_relation_type_links_no_issue(self):
        """관계타입 있는 링크들 → relation_types 이슈 없음."""
        result = _quick_check(FULL_CONTENT)
        relation_issues = [i for i in result["issues"] if "관계 타입" in i]
        assert len(relation_issues) == 0

    def test_score_max_8(self):
        """점수는 최대 8점."""
        result = _quick_check(FULL_CONTENT)
        assert result["score"] <= 8

    def test_result_has_required_keys(self):
        result = _quick_check(FULL_CONTENT)
        for key in ("score", "pass", "issues", "details", "method"):
            assert key in result

    def test_method_is_quick_check(self):
        result = _quick_check(FULL_CONTENT)
        assert result["method"] == "quick_check"

    def test_empty_related_section_no_false_relation_issue(self):
        """관련 개념 섹션에 링크가 없으면 관계타입 이슈 미발생."""
        content = """## 정의
정의

## 요약
요약

## 상세 설명
상세

## 인과 관계
인과

## 관련 개념
(없음)

## 스펙 근거
3GPP

## 소스
sources
"""
        result = _quick_check(content)
        relation_issues = [i for i in result["issues"] if "관계 타입" in i]
        assert len(relation_issues) == 0


# ──────────────────────────────────────────────
# _compute_delta 테스트
# ──────────────────────────────────────────────

class TestComputeDelta:
    def test_net_fixed_counts_newly_passed(self):
        before = {
            "pages": [
                {"path": "entities/A.md", "score": 4},
                {"path": "entities/B.md", "score": 3},
            ]
        }
        after = {
            "results": [
                {"path": "entities/A.md", "passed": True, "delta": 4},
                {"path": "entities/B.md", "passed": False, "delta": 1},
            ]
        }
        delta = _compute_delta(before, after)
        assert delta["net_fixed"] == 1
        assert "entities/A.md" in delta["newly_passed"]

    def test_still_failed_recorded(self):
        before = {
            "pages": [
                {"path": "entities/A.md", "score": 3},
                {"path": "entities/B.md", "score": 4},
            ]
        }
        after = {
            "results": [
                {"path": "entities/A.md", "passed": False, "delta": 1},
                {"path": "entities/B.md", "passed": False, "delta": 0},
            ]
        }
        delta = _compute_delta(before, after)
        assert "entities/A.md" in delta["still_failed"]
        assert "entities/B.md" in delta["still_failed"]

    def test_avg_score_delta_computed(self):
        before = {
            "pages": [
                {"path": "entities/A.md", "score": 4},
                {"path": "entities/B.md", "score": 3},
            ]
        }
        after = {
            "results": [
                {"path": "entities/A.md", "passed": True, "delta": 4},
                {"path": "entities/B.md", "passed": False, "delta": 2},
            ]
        }
        delta = _compute_delta(before, after)
        # avg = (4 + 2) / 2 = 3.0
        assert delta["avg_score_delta"] == 3.0

    def test_all_passed_net_fixed_equals_before_count(self):
        before = {
            "pages": [
                {"path": "entities/A.md", "score": 4},
                {"path": "entities/B.md", "score": 5},
            ]
        }
        after = {
            "results": [
                {"path": "entities/A.md", "passed": True, "delta": 3},
                {"path": "entities/B.md", "passed": True, "delta": 2},
            ]
        }
        delta = _compute_delta(before, after)
        assert delta["net_fixed"] == 2
        assert delta["still_failed"] == []

    def test_no_delta_entries_avg_zero(self):
        """delta 없는 결과 → avg_score_delta = 0."""
        before = {"pages": [{"path": "entities/A.md", "score": 4}]}
        after = {
            "results": [
                {"path": "entities/A.md", "passed": True},  # delta 없음
            ]
        }
        delta = _compute_delta(before, after)
        assert delta["avg_score_delta"] == 0


# ──────────────────────────────────────────────
# EvalHistory 테스트
# ──────────────────────────────────────────────

class TestEvalHistory:
    def test_new_session_returns_id(self, tmp_path):
        path = tmp_path / "eval_history.json"
        history = EvalHistory(path)
        session_id = history.new_session()
        assert isinstance(session_id, str)
        assert len(session_id) > 0

    def test_add_round_recorded(self, tmp_path):
        path = tmp_path / "eval_history.json"
        history = EvalHistory(path)
        session_id = history.new_session()

        before = {"pages": [{"path": "entities/A.md", "score": 4}]}
        after = {"results": [{"path": "entities/A.md", "passed": True, "delta": 3}]}
        change = {"root_cause": "테스트", "user_confirmed": True}

        history.add_round(session_id, before, change, after)
        history.save()

        data = json.loads(path.read_text(encoding="utf-8"))
        sessions = data["sessions"]
        assert len(sessions) == 1
        assert len(sessions[0]["rounds"]) == 1

    def test_close_session_adds_summary(self, tmp_path):
        path = tmp_path / "eval_history.json"
        history = EvalHistory(path)
        session_id = history.new_session()

        before = {"pages": [{"path": "entities/A.md", "score": 4}]}
        after = {
            "results": [{"path": "entities/A.md", "passed": True, "delta": 3}],
            "improved_count": 1,
            "still_failed_count": 0,
        }
        history.add_round(session_id, before, {"user_confirmed": True}, after)
        history.close_session(session_id)
        history.save()

        data = json.loads(path.read_text(encoding="utf-8"))
        summary = data["sessions"][0]["summary"]
        assert summary is not None
        assert "total_rounds" in summary

    def test_empty_session_summary_has_note(self, tmp_path):
        path = tmp_path / "eval_history.json"
        history = EvalHistory(path)
        session_id = history.new_session()
        history.close_session(session_id, note="테스트 메모")

        summary = history.get_session_summary(session_id)
        assert summary is not None
        assert "note" in summary

    def test_multiple_rounds_accumulated(self, tmp_path):
        path = tmp_path / "eval_history.json"
        history = EvalHistory(path)
        session_id = history.new_session()

        for i in range(3):
            before = {"pages": [{"path": f"entities/P{i}.md", "score": 3}]}
            after = {
                "results": [{"path": f"entities/P{i}.md", "passed": True, "delta": 4}],
                "improved_count": 1,
                "still_failed_count": 0,
            }
            history.add_round(session_id, before, {}, after)

        history.close_session(session_id)
        summary = history.get_session_summary(session_id)
        assert summary["total_rounds"] == 3

    def test_load_existing_history(self, tmp_path):
        """기존 eval_history.json이 있으면 로드한다."""
        path = tmp_path / "eval_history.json"
        existing = {"sessions": [{"id": "2026-01-01T00:00:00", "rounds": [], "summary": None}]}
        path.write_text(json.dumps(existing), encoding="utf-8")

        history = EvalHistory(path)
        session_id = history.new_session()

        # 기존 세션 + 새 세션 = 2개
        history.save()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["sessions"]) == 2


# ──────────────────────────────────────────────
# _apply_prompt_fix — 중복 패치 전체 내용 비교
# ──────────────────────────────────────────────

class TestApplyPromptFix:
    """_apply_prompt_fix의 중복 검사가 전체 내용 기준으로 동작함을 검증."""

    def test_new_patch_is_written(self, tmp_path):
        """patches.md가 없을 때 첫 패치가 저장된다."""
        patches_path = tmp_path / "generator_patches.md"

        import wiki_builder.evaluate as eval_mod
        import wiki_builder.prompt_loader as loader_mod

        with patch.object(loader_mod, '_SUB_AGENTS_DIR', tmp_path):
            _apply_prompt_fix("GENERATOR_SYSTEM", "규칙 A: 섹션 헤더를 포함할 것")

        assert patches_path.exists()
        assert "규칙 A: 섹션 헤더를 포함할 것" in patches_path.read_text(encoding="utf-8")

    def test_duplicate_patch_skipped(self, tmp_path):
        """이미 동일 내용이 있으면 중복 저장 안 함."""
        patches_path = tmp_path / "generator_patches.md"
        patches_path.write_text("규칙 A: 섹션 헤더를 포함할 것", encoding="utf-8")

        import wiki_builder.prompt_loader as loader_mod

        with patch.object(loader_mod, '_SUB_AGENTS_DIR', tmp_path):
            _apply_prompt_fix("GENERATOR_SYSTEM", "규칙 A: 섹션 헤더를 포함할 것")

        content = patches_path.read_text(encoding="utf-8")
        # 중복이면 PATCH 구분자가 추가되지 않아야 함
        assert "---PATCH---" not in content

    def test_prefix_match_does_not_skip(self, tmp_path):
        """기존 패치의 앞 일부만 일치해도 전체 비교이므로 중복 아님 → 저장된다.

        이전 버그(앞 100자 비교)에서는 이 케이스가 중복으로 오탐될 수 있었다.
        현재 코드(전체 비교)에서는 전체 내용이 달라야만 새 패치로 추가된다.
        """
        patches_path = tmp_path / "generator_patches.md"
        short_content = "규칙 A: 섹션 헤더를 포함할 것"
        patches_path.write_text(short_content, encoding="utf-8")

        # 기존 패치 앞부분과 같지만 더 긴 새 패치
        longer_content = "규칙 A: 섹션 헤더를 포함할 것 — 추가 조건: 반드시 두 줄 이상 작성"

        import wiki_builder.prompt_loader as loader_mod

        with patch.object(loader_mod, '_SUB_AGENTS_DIR', tmp_path):
            _apply_prompt_fix("GENERATOR_SYSTEM", longer_content)

        content = patches_path.read_text(encoding="utf-8")
        # 전체 내용이 다르므로 새 패치가 추가되어야 함
        assert "---PATCH---" in content
        assert longer_content in content

    def test_multiple_patches_accumulated(self, tmp_path):
        """다른 내용의 패치 2개는 각각 별도로 저장된다."""
        patches_path = tmp_path / "generator_patches.md"

        import wiki_builder.prompt_loader as loader_mod

        with patch.object(loader_mod, '_SUB_AGENTS_DIR', tmp_path):
            _apply_prompt_fix("GENERATOR_SYSTEM", "규칙 A")
            _apply_prompt_fix("GENERATOR_SYSTEM", "규칙 B")

        content = patches_path.read_text(encoding="utf-8")
        assert "규칙 A" in content
        assert "규칙 B" in content
        assert content.count("---PATCH---") == 1  # 첫 번째 구분자만 (두 패치 사이)


# ──────────────────────────────────────────────
# run_evaluate — LLM 실패/파싱 실패 시 continue
# ──────────────────────────────────────────────

class TestRunEvaluateContinueOnError:
    """Evaluator LLM 실패 또는 파싱 실패 시 break가 아닌 continue로 다음 라운드를 시도한다."""

    def _make_plan(self, pages):
        return {"pages": pages}

    def _base_failed_page(self, path="entities/PUSCH.md"):
        return {
            "path": path,
            "score": 4,
            "issues": ["누락 섹션: 상세 설명"],
            "reason": "low_score",
            "content": "## 정의\n내용",
        }

    def _make_sub_agents_dir(self, tmp_path):
        """테스트용 최소 sub_agents 디렉토리 생성 (evaluator.md, generator.md)."""
        sub_agents_dir = tmp_path / "sub_agents"
        sub_agents_dir.mkdir()
        (sub_agents_dir / "evaluator.md").write_text(
            "evaluator system\n---USER---\n{failed_pages}\n{current_prompt}\n{current_user_prompt}",
            encoding="utf-8"
        )
        (sub_agents_dir / "generator.md").write_text(
            "generator system\n---USER---\ngenerator user {spec_content} {existing_pages}",
            encoding="utf-8"
        )
        return sub_agents_dir

    def test_llm_failure_continues_to_next_round(self, tmp_path):
        """첫 라운드 LLM 호출 실패 → 두 번째 라운드에서 성공 → 정상 종료."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        plan_path = tmp_path / "plan.json"
        eval_log = str(tmp_path / "eval.log")

        # plan에 generated=True인 페이지가 있지만 wiki_dir에 파일이 없음
        # → _collect_failed_pages가 file_path.exists() 체크에서 skip → from_existing = []
        page = {
            "path": "entities/PUSCH.md",
            "description": "PUSCH",
            "generated": True,
            "linked": True,
            "sources": [],
        }
        plan = self._make_plan([page])

        call_count = [0]

        def mock_llm(system, user, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return "[LLM 호출 실패] 연결 오류"
            # 두 번째 이후: fix_target=checker → break
            return json.dumps({
                "root_cause": "테스트",
                "failure_pattern": "",
                "fix_target": "checker",
                "affected_pages": [],
                "confidence": "high",
            })

        def mock_extract(page):
            return "스펙 내용"

        failed_pages = [self._base_failed_page()]

        from wiki_builder.evaluate import run_evaluate
        import wiki_builder.prompt_loader as loader_mod

        sub_agents_dir = self._make_sub_agents_dir(tmp_path)

        with patch.object(loader_mod, '_SUB_AGENTS_DIR', sub_agents_dir):
            run_evaluate(
                plan=plan,
                wiki_dir=str(wiki_dir),
                plan_path=str(plan_path),
                eval_log=eval_log,
                call_llm=mock_llm,
                extract_spec_fn=mock_extract,
                backend="claude",
                initial_failed=failed_pages,
            )

        # LLM이 2번 이상 호출됨 = 첫 실패 후 다음 라운드로 continue했음
        assert call_count[0] >= 2

    def test_parse_failure_continues_to_next_round(self, tmp_path):
        """첫 라운드 파싱 실패 → 두 번째 라운드에서 성공 → 정상 종료."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        plan_path = tmp_path / "plan.json"
        eval_log = str(tmp_path / "eval.log")

        page = {
            "path": "entities/PUSCH.md",
            "description": "PUSCH",
            "generated": True,
            "linked": True,
            "sources": [],
        }
        plan = self._make_plan([page])

        call_count = [0]

        def mock_llm(system, user, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return "이것은 JSON이 아닙니다 --- broken response"
            return json.dumps({
                "root_cause": "테스트",
                "failure_pattern": "",
                "fix_target": "checker",
                "affected_pages": [],
                "confidence": "high",
            })

        def mock_extract(page):
            return "스펙 내용"

        failed_pages = [self._base_failed_page()]

        from wiki_builder.evaluate import run_evaluate
        import wiki_builder.prompt_loader as loader_mod

        sub_agents_dir = self._make_sub_agents_dir(tmp_path)

        with patch.object(loader_mod, '_SUB_AGENTS_DIR', sub_agents_dir):
            run_evaluate(
                plan=plan,
                wiki_dir=str(wiki_dir),
                plan_path=str(plan_path),
                eval_log=eval_log,
                call_llm=mock_llm,
                extract_spec_fn=mock_extract,
                backend="claude",
                initial_failed=failed_pages,
            )

        # 파싱 실패 후 다음 라운드로 continue했음 = 2번 이상 호출
        assert call_count[0] >= 2


# ──────────────────────────────────────────────
# check_quality — feature_hint 파라미터 전달
# ──────────────────────────────────────────────

class TestCheckQualityFeatureHint:
    """check_quality()가 feature_hint를 LLM user_msg에 포함시켜 전달한다."""

    def _make_sub_agents_dir(self, tmp_path):
        sub_agents_dir = tmp_path / "sub_agents"
        sub_agents_dir.mkdir()
        (sub_agents_dir / "checker.md").write_text(
            "checker system\n---USER---\n"
            "{page_content}\n{spec_content}\n{feature_hint}",
            encoding="utf-8"
        )
        return sub_agents_dir

    def test_feature_hint_passed_to_llm(self, tmp_path):
        """feature_hint가 LLM에 전달되는 user_msg에 포함되어야 한다."""
        sub_agents_dir = self._make_sub_agents_dir(tmp_path)
        received = {}

        def capture_llm(system, user, **kwargs):
            received['user'] = user
            return '{"score": 8, "pass": true, "issues": []}'

        from wiki_builder.evaluate import check_quality
        import wiki_builder.prompt_loader as loader_mod

        with patch.object(loader_mod, '_SUB_AGENTS_DIR', sub_agents_dir):
            result = check_quality(
                content="## 정의\n내용",
                spec_content="스펙 내용",
                call_llm=capture_llm,
                backend="claude",
                feature_hint="Feature: PUSCH power control",
            )

        assert "Feature: PUSCH power control" in received.get('user', '')

    def test_empty_feature_hint_still_works(self, tmp_path):
        """feature_hint가 빈 문자열이어도 정상 동작한다."""
        sub_agents_dir = self._make_sub_agents_dir(tmp_path)

        def mock_llm(system, user, **kwargs):
            return '{"score": 7, "pass": false, "issues": ["테스트 이슈"]}'

        from wiki_builder.evaluate import check_quality
        import wiki_builder.prompt_loader as loader_mod

        with patch.object(loader_mod, '_SUB_AGENTS_DIR', sub_agents_dir):
            result = check_quality(
                content="## 정의\n내용",
                spec_content="스펙",
                call_llm=mock_llm,
                backend="claude",
                feature_hint="",
            )

        assert result["score"] == 7
        assert result["pass"] is False

    def test_llm_failure_returns_quick_check_result(self, tmp_path):
        """LLM 실패 시 quick_check 결과를 반환한다."""
        sub_agents_dir = self._make_sub_agents_dir(tmp_path)

        def failing_llm(system, user, **kwargs):
            return "[LLM 호출 실패] 연결 오류"

        from wiki_builder.evaluate import check_quality
        import wiki_builder.prompt_loader as loader_mod

        with patch.object(loader_mod, '_SUB_AGENTS_DIR', sub_agents_dir):
            result = check_quality(
                content="## 정의\n내용",
                spec_content="스펙",
                call_llm=failing_llm,
                backend="claude",
                feature_hint="hint",
            )

        # LLM 실패 시 quick_check 결과 — method 키 확인
        assert "score" in result
        assert "pass" in result
