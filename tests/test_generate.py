"""
test_generate.py — generate.py 단위 테스트

테스트 대상:
- _detect_hallucination(): 5어절 이상 구절 4회 이상 반복 → 문자열 반환 (None이 아님)
- _detect_hallucination(): 정상 텍스트 → None 반환
- run_generate(): generated=True 플래그 업데이트
- run_generate(): hallucination 감지 시 failed 반환
- _generate_page(): 품질 합격 시 첫 시도에서 즉시 저장/리턴
- _generate_page(): 품질 불합격 시 최대 QUALITY_RETRY_MAX 회 재시도
- _generate_page(): 모든 시도 불합격 시 failed=True 반환 (파일 저장 없음)
- _generate_page(): check_quality_fn에 feature_hint 전달

LLM 호출 mock — 실제 API 호출 없음.
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


from wiki_builder.generate import _detect_hallucination, run_generate, QUALITY_RETRY_MAX


# ──────────────────────────────────────────────
# _detect_hallucination 테스트
# 반환값: 없으면 None, 있으면 의심 구절 문자열
# n-gram 범위: 5~10어절, 4회 이상 반복 시 감지
# ──────────────────────────────────────────────

class TestDetectHallucination:
    def test_normal_text_returns_none(self):
        """정상 텍스트는 None을 반환한다."""
        text = (
            "PUSCH는 Physical Uplink Shared Channel의 약자로 5G NR에서 사용된다. "
            "PUSCH는 데이터 전송에 사용되는 채널이다. "
            "UCI를 포함할 수 있으며 다양한 MCS를 지원한다."
        )
        assert _detect_hallucination(text) is None

    def test_short_text_returns_none(self):
        """단어 수가 15 미만이면 None."""
        text = "짧은 텍스트 네 단어"
        assert _detect_hallucination(text) is None

    def test_repeated_5gram_4_times_returns_string(self):
        """5어절 구절이 4회 이상 반복되면 해당 구절(문자열)을 반환한다."""
        phrase = "PUSCH uplink channel data transmission"  # 5 words
        text = " ".join([phrase] * 4 + ["additional unique content to reach fifteen words total here"])
        result = _detect_hallucination(text)
        assert result is not None
        assert isinstance(result, str)

    def test_repeated_5gram_3_times_returns_none(self):
        """5어절 구절이 3회만 반복 → None."""
        phrase = "PUSCH uplink channel data transmission"  # 5 words
        text = " ".join([phrase] * 3 + ["many other different unique words to make it longer than fifteen total"])
        assert _detect_hallucination(text) is None

    def test_headers_excluded_from_check(self):
        """섹션 헤더(##)는 검사에서 제외된다."""
        header_repeat = "\n".join(["## PUSCH uplink channel data transmission"] * 10)
        text = header_repeat + "\nactual body content here with diverse and unique words no repetition"
        assert _detect_hallucination(text) is None

    def test_wikilink_lines_excluded(self):
        """- [[ 로 시작하는 관련 개념 목록 라인은 제외."""
        link_repeat = "\n".join(["- [[PUSCH (affects)]]"] * 10)
        text = link_repeat + "\nbody text with diverse content and no repetition hallucination should not occur here"
        assert _detect_hallucination(text) is None

    def test_detected_gram_is_substring_of_original(self):
        """감지된 구절은 원본 텍스트에 포함되어야 한다."""
        phrase = "PUSCH uplink channel data transmission"  # 5 words
        text = " ".join([phrase] * 4 + ["more words here to ensure fifteen total words are present"])
        result = _detect_hallucination(text)
        assert result is not None
        # 반환된 구절의 첫 단어가 원본에 있어야 함
        assert result.split()[0] in text


# ──────────────────────────────────────────────
# run_generate 통합 테스트
# ──────────────────────────────────────────────

class TestRunGenerate:
    def _make_plan(self, pages):
        return {"pages": pages}

    def _base_page(self, path="entities/PUSCH.md"):
        return {
            "path": path,
            "description": "PUSCH 채널 설명",
            "generated": False,
            "linked": False,
            "sources": [{"file": "sources/38211.docx", "sections": ["6.3.1"]}],
        }

    def test_successful_generation_sets_generated_true(self, tmp_path):
        """LLM이 정상 응답하면 page["generated"] = True."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        plan_path = tmp_path / "plan.json"

        page = self._base_page()
        plan = self._make_plan([page])

        def mock_llm(system, user, **kwargs):
            return """## 정의
PUSCH는 Physical Uplink Shared Channel이다.

## 요약
요약 내용이 여기 있습니다.

## 상세 설명
상세한 설명이 여기 있습니다.

## 인과 관계
인과 관계 내용

## 관련 개념
- [[UCI (uses)]]

## 스펙 근거
3GPP TS 38.211

## 소스
sources/38211.docx
"""

        def mock_extract(page):
            return "섹션 6.3.1 내용"

        def mock_check(content, spec, call_llm_fn, **kwargs):
            return {"score": 8, "pass": True, "issues": []}

        # parse_38822 import 오류 방지
        with patch('wiki_builder.generate._generate_page') as mock_gen:
            mock_gen.return_value = {"path": page["path"], "failed": False}

            run_generate(
                plan=plan,
                wiki_dir=str(wiki_dir),
                plan_path=str(plan_path),
                call_llm=mock_llm,
                extract_spec_fn=mock_extract,
                check_quality_fn=mock_check,
                backend="claude",
                max_workers=1,
            )

        assert page["generated"] is True

    def test_failed_generation_page_not_marked_generated(self, tmp_path):
        """LLM 실패(hallucination 등) 시 page["generated"]는 False 유지."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        plan_path = tmp_path / "plan.json"

        page = self._base_page()
        plan = self._make_plan([page])

        def mock_llm(system, user, **kwargs):
            return "[LLM 호출 실패] 연결 오류"

        def mock_extract(page):
            return "스펙 내용"

        def mock_check(content, spec, call_llm_fn, **kwargs):
            return {"score": 3, "pass": False, "issues": ["누락 섹션"]}

        with patch('wiki_builder.generate._generate_page') as mock_gen:
            mock_gen.return_value = {"path": page["path"], "failed": True, "reason": "llm_fail"}

            failed = run_generate(
                plan=plan,
                wiki_dir=str(wiki_dir),
                plan_path=str(plan_path),
                call_llm=mock_llm,
                extract_spec_fn=mock_extract,
                check_quality_fn=mock_check,
                backend="claude",
                max_workers=1,
            )

        assert page["generated"] is False
        assert len(failed) > 0

    def test_already_generated_page_skipped(self, tmp_path):
        """generated=True인 페이지는 스킵."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        plan_path = tmp_path / "plan.json"

        page = self._base_page()
        page["generated"] = True
        plan = self._make_plan([page])

        call_count = [0]

        def mock_llm(system, user, **kwargs):
            call_count[0] += 1
            return "content"

        def mock_extract(p):
            return "spec"

        def mock_check(content, spec, fn, **kwargs):
            return {"score": 8, "pass": True, "issues": []}

        with patch('wiki_builder.generate._generate_page') as mock_gen:
            run_generate(
                plan=plan,
                wiki_dir=str(wiki_dir),
                plan_path=str(plan_path),
                call_llm=mock_llm,
                extract_spec_fn=mock_extract,
                check_quality_fn=mock_check,
                max_workers=1,
            )
            mock_gen.assert_not_called()


# ──────────────────────────────────────────────
# _generate_page 직접 테스트 (품질 재시도 / feature_hint)
# ──────────────────────────────────────────────

class TestGeneratePage:
    """_generate_page() 내부 동작 테스트."""

    def _make_sub_agents_dir(self, tmp_path):
        sub_agents_dir = tmp_path / "sub_agents"
        sub_agents_dir.mkdir()
        (sub_agents_dir / "generator.md").write_text(
            "generator system\n---USER---\n"
            "{page_path}\n{page_description}\n{feature_hint}\n{spec_content}\n{wiki_page_list}",
            encoding="utf-8"
        )
        return sub_agents_dir

    def _base_page(self, path="entities/PUSCH.md"):
        return {
            "path": path,
            "description": "PUSCH 채널",
            "generated": False,
            "linked": False,
            "sources": [{"file": "sources/38211.docx", "sections": ["6.3.1"]}],
        }

    def test_quality_retry_max_exceeded_returns_failed_true(self, tmp_path):
        """QUALITY_RETRY_MAX 회 모두 품질 불합격 → failed=True, 파일 미생성."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        sub_agents_dir = self._make_sub_agents_dir(tmp_path)

        page = self._base_page()

        def mock_llm(system, user, **kwargs):
            return "## 정의\n내용"

        def mock_extract(p):
            return "스펙 내용"

        def always_fail_check(content, spec, call_llm_fn, **kwargs):
            return {"score": 3, "pass": False, "issues": ["누락 섹션"]}

        from wiki_builder.generate import _generate_page
        import wiki_builder.prompt_loader as loader_mod
        from unittest.mock import patch as _patch

        with _patch.object(loader_mod, '_SUB_AGENTS_DIR', sub_agents_dir):
            # parse_38822 모듈 모킹 (feature_hint 생성 부분)
            with _patch('wiki_builder.generate._detect_hallucination', return_value=None):
                with _patch('wiki_builder.generate._verify_hallucination_with_llm', return_value=False):
                    result = _generate_page(
                        page=page,
                        wiki_dir=str(wiki_dir),
                        wiki_page_list="entities/PUSCH.md",
                        call_llm=mock_llm,
                        extract_spec_fn=mock_extract,
                        check_quality_fn=always_fail_check,
                        backend="claude",
                        feature_list=None,
                    )

        assert result["failed"] is True
        assert "품질 기준 미달" in result.get("reason", "")
        # 파일이 생성되지 않아야 함
        assert not (wiki_dir / "entities" / "PUSCH.md").exists()

    def test_quality_pass_on_first_attempt_saves_file(self, tmp_path):
        """첫 시도 품질 합격 → 파일 저장 후 failed=False."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        sub_agents_dir = self._make_sub_agents_dir(tmp_path)

        page = self._base_page()
        good_content = "## 정의\nPUSCH 채널"

        def mock_llm(system, user, **kwargs):
            return good_content

        def mock_extract(p):
            return "스펙"

        def passing_check(content, spec, call_llm_fn, **kwargs):
            return {"score": 8, "pass": True, "issues": []}

        from wiki_builder.generate import _generate_page
        import wiki_builder.prompt_loader as loader_mod
        from unittest.mock import patch as _patch

        with _patch.object(loader_mod, '_SUB_AGENTS_DIR', sub_agents_dir):
            with _patch('wiki_builder.generate._detect_hallucination', return_value=None):
                with _patch('wiki_builder.generate._verify_hallucination_with_llm', return_value=False):
                    result = _generate_page(
                        page=page,
                        wiki_dir=str(wiki_dir),
                        wiki_page_list="entities/PUSCH.md",
                        call_llm=mock_llm,
                        extract_spec_fn=mock_extract,
                        check_quality_fn=passing_check,
                        backend="claude",
                        feature_list=None,
                    )

        assert result["failed"] is False
        assert (wiki_dir / "entities" / "PUSCH.md").exists()

    def test_feature_hint_forwarded_to_check_quality_fn(self, tmp_path):
        """feature_list가 없을 때도 check_quality_fn에 feature_hint 키워드 인자가 전달된다."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        sub_agents_dir = self._make_sub_agents_dir(tmp_path)

        page = self._base_page()
        received_kwargs = {}

        def mock_llm(system, user, **kwargs):
            return "## 정의\n내용"

        def mock_extract(p):
            return "스펙"

        def capturing_check(content, spec, call_llm_fn, **kwargs):
            received_kwargs.update(kwargs)
            return {"score": 8, "pass": True, "issues": []}

        from wiki_builder.generate import _generate_page
        import wiki_builder.prompt_loader as loader_mod
        from unittest.mock import patch as _patch

        with _patch.object(loader_mod, '_SUB_AGENTS_DIR', sub_agents_dir):
            with _patch('wiki_builder.generate._detect_hallucination', return_value=None):
                with _patch('wiki_builder.generate._verify_hallucination_with_llm', return_value=False):
                    _generate_page(
                        page=page,
                        wiki_dir=str(wiki_dir),
                        wiki_page_list="",
                        call_llm=mock_llm,
                        extract_spec_fn=mock_extract,
                        check_quality_fn=capturing_check,
                        backend="claude",
                        feature_list=None,
                    )

        # feature_hint 키워드가 check_quality_fn에 전달되어야 함
        assert "feature_hint" in received_kwargs

    def test_quality_retry_count_equals_quality_retry_max(self, tmp_path):
        """항상 불합격일 때 check_quality_fn 호출 횟수가 정확히 QUALITY_RETRY_MAX번이다."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        sub_agents_dir = self._make_sub_agents_dir(tmp_path)

        page = self._base_page()
        check_call_count = [0]

        def mock_llm(system, user, **kwargs):
            return "## 정의\n내용"

        def mock_extract(p):
            return "스펙"

        def counting_check(content, spec, call_llm_fn, **kwargs):
            check_call_count[0] += 1
            return {"score": 4, "pass": False, "issues": ["낮은 점수"]}

        from wiki_builder.generate import _generate_page, QUALITY_RETRY_MAX
        import wiki_builder.prompt_loader as loader_mod
        from unittest.mock import patch as _patch

        with _patch.object(loader_mod, '_SUB_AGENTS_DIR', sub_agents_dir):
            with _patch('wiki_builder.generate._detect_hallucination', return_value=None):
                with _patch('wiki_builder.generate._verify_hallucination_with_llm', return_value=False):
                    _generate_page(
                        page=page,
                        wiki_dir=str(wiki_dir),
                        wiki_page_list="",
                        call_llm=mock_llm,
                        extract_spec_fn=mock_extract,
                        check_quality_fn=counting_check,
                        backend="claude",
                        feature_list=None,
                    )

        assert check_call_count[0] == QUALITY_RETRY_MAX
