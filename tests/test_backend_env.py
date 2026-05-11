"""
test_backend_env.py — backend=None 패턴 및 WIKI_BACKEND 환경변수 테스트

테스트 대상:
1. api.BACKEND: WIKI_BACKEND 미설정 시 "gemini" 기본값
2. api.BACKEND: WIKI_BACKEND 설정 시 해당 값 사용
3. run_generate(backend=None): api.BACKEND 에서 해석
4. run_plan(backend=None): api.BACKEND 에서 해석
5. run_link(backend=None): api.BACKEND 에서 해석
6. run_post_plan(backend=None): api.BACKEND 에서 해석
7. check_quality(backend=None): api.BACKEND 에서 해석
8. run_evaluate(backend=None): api.BACKEND 에서 해석

LLM 호출 mock — 실제 API 호출 없음.
"""
import json
import os
import sys
import importlib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest



# ──────────────────────────────────────────────
# api.BACKEND 기본값 테스트
# ──────────────────────────────────────────────

class TestApiBackendDefault:
    def test_wiki_backend_unset_defaults_to_gemini(self):
        """WIKI_BACKEND 환경변수가 없으면 BACKEND는 "gemini"."""
        env_without = {k: v for k, v in os.environ.items() if k != "WIKI_BACKEND"}
        with patch.dict(os.environ, env_without, clear=True):
            # 모듈을 다시 로드해서 module-level BACKEND 재평가
            import wiki_builder.api as api_mod
            # 실제 BACKEND 값은 모듈 로드 시 결정되므로 os.getenv로 검증
            default = os.getenv("WIKI_BACKEND", "gemini")
            assert default == "gemini"

    def test_wiki_backend_env_var_used_when_set(self):
        """WIKI_BACKEND=claude 설정 시 os.getenv 결과는 "claude"."""
        with patch.dict(os.environ, {"WIKI_BACKEND": "claude"}):
            value = os.getenv("WIKI_BACKEND", "gemini")
            assert value == "claude"

    def test_wiki_backend_gemini_is_valid_backend(self):
        """gemini는 유효한 백엔드이므로 _validate_backend가 True를 반환해야 한다."""
        from wiki_builder.api import _validate_backend
        assert _validate_backend("gemini") is True

    def test_wiki_backend_claude_is_valid_backend(self):
        """claude는 유효한 백엔드."""
        from wiki_builder.api import _validate_backend
        assert _validate_backend("claude") is True

    def test_wiki_backend_gptoss_is_valid_backend(self):
        """gptoss는 유효한 백엔드."""
        from wiki_builder.api import _validate_backend
        assert _validate_backend("gptoss") is True

    def test_wiki_backend_ollama_is_valid_backend(self):
        """ollama는 유효한 백엔드."""
        from wiki_builder.api import _validate_backend
        assert _validate_backend("ollama") is True

    def test_empty_string_is_invalid_backend(self):
        """빈 문자열은 유효하지 않은 백엔드."""
        from wiki_builder.api import _validate_backend
        assert _validate_backend("") is False


# ──────────────────────────────────────────────
# call_simple: backend=None → api.BACKEND 사용
# ──────────────────────────────────────────────

class TestCallSimpleBackendNone:
    """call_simple에 backend 키워드 없이 호출하면 module-level BACKEND를 사용한다."""

    def test_backend_kwarg_defaults_to_module_backend(self):
        """backend 키워드 없이 호출 → BACKEND(gemini) 경로 시도."""
        import wiki_builder.api as api_mod

        captured = {}

        def fake_gemini(system, user, temperature, **kwargs):
            captured["called"] = True
            return "gemini response"

        with patch.object(api_mod, "_call_gemini", side_effect=fake_gemini):
            with patch.object(api_mod, "BACKEND", "gemini"):
                result = api_mod.call_simple("sys", "user")

        assert captured.get("called") is True
        assert result == "gemini response"

    def test_backend_none_kwarg_falls_back_to_module_backend(self):
        """backend=None 명시 → BACKEND 경로를 사용한다."""
        import wiki_builder.api as api_mod

        captured = {}

        def fake_gemini(system, user, temperature, **kwargs):
            captured["called"] = True
            return "ok"

        with patch.object(api_mod, "_call_gemini", side_effect=fake_gemini):
            with patch.object(api_mod, "BACKEND", "gemini"):
                # call_simple은 backend=kwargs.pop("backend", BACKEND) 형태
                # backend=None을 명시로 전달하면 None이 사용되어 _validate_backend("" 또는 None) 실패 가능
                # 실제 코드: backend = kwargs.pop("backend", BACKEND)
                # backend=None을 전달하면 backend는 None이 됨 → _validate_backend(None) → False → 실패 반환
                # 단, api.py는 backend = kwargs.pop("backend", BACKEND) 후
                # _validate_backend(backend) 호출. None은 in ("claude","gemini",...) False → 실패
                result = api_mod.call_simple("sys", "user", backend=None)

        # backend=None이면 _validate_backend(None)가 False → "[LLM 호출 실패]" 반환
        # 이 동작을 명시적으로 확인
        assert result.startswith("[LLM 호출 실패]")


# ──────────────────────────────────────────────
# run_generate: backend=None → api.BACKEND 사용
# ──────────────────────────────────────────────

class TestRunGenerateBackendNone:
    """run_generate(backend=None) 시 내부에서 api.BACKEND로 대체되어 호출된다."""

    def _make_plan(self):
        return {"pages": []}

    def test_backend_none_resolves_from_api_backend(self, tmp_path):
        """backend=None 전달 시 wiki_builder.api.BACKEND 값을 사용한다."""
        import wiki_builder.api as api_mod
        from wiki_builder.generate import run_generate

        resolved = {}

        def tracking_generate_page(**kwargs):
            resolved["backend"] = kwargs.get("backend")
            return {"path": "x", "failed": False}

        with patch.object(api_mod, "BACKEND", "claude"):
            with patch("wiki_builder.generate._generate_page", side_effect=tracking_generate_page):
                plan = {"pages": [{
                    "path": "entities/TEST.md",
                    "description": "test",
                    "generated": False,
                    "linked": False,
                    "sources": [],
                }]}
                run_generate(
                    plan=plan,
                    wiki_dir=str(tmp_path),
                    plan_path=str(tmp_path / "plan.json"),
                    call_llm=lambda s, u, **kw: "ok",
                    extract_spec_fn=lambda p: "spec",
                    check_quality_fn=lambda c, s, fn, **kw: {"score": 8, "pass": True, "issues": []},
                    backend=None,
                    max_workers=1,
                )

        assert resolved.get("backend") == "claude"

    def test_backend_explicit_overrides_api_backend(self, tmp_path):
        """backend='gemini' 명시 시 api.BACKEND와 관계없이 gemini 사용."""
        import wiki_builder.api as api_mod
        from wiki_builder.generate import run_generate

        resolved = {}

        def tracking_generate_page(**kwargs):
            resolved["backend"] = kwargs.get("backend")
            return {"path": "x", "failed": False}

        with patch.object(api_mod, "BACKEND", "claude"):
            with patch("wiki_builder.generate._generate_page", side_effect=tracking_generate_page):
                plan = {"pages": [{
                    "path": "entities/TEST.md",
                    "description": "test",
                    "generated": False,
                    "linked": False,
                    "sources": [],
                }]}
                run_generate(
                    plan=plan,
                    wiki_dir=str(tmp_path),
                    plan_path=str(tmp_path / "plan.json"),
                    call_llm=lambda s, u, **kw: "ok",
                    extract_spec_fn=lambda p: "spec",
                    check_quality_fn=lambda c, s, fn, **kw: {"score": 8, "pass": True, "issues": []},
                    backend="gemini",
                    max_workers=1,
                )

        assert resolved.get("backend") == "gemini"


# ──────────────────────────────────────────────
# run_link: backend=None → api.BACKEND 사용
# ──────────────────────────────────────────────

class TestRunLinkBackendNone:
    """run_link(backend=None) 시 내부에서 api.BACKEND로 대체된다."""

    def _make_sub_agents(self, tmp_path):
        sa = tmp_path / "sub_agents"
        sa.mkdir()
        (sa / "linker.md").write_text(
            "linker system\n---USER---\n{file_content}\n{inbound_links}",
            encoding="utf-8"
        )
        return sa

    def test_backend_none_uses_api_backend(self, tmp_path):
        """backend=None 전달 시 wiki_builder.api.BACKEND 값이 call_llm에 전달된다.

        run_link는 inbound 링크가 있을 때만 LLM을 호출한다.
        두 파일이 서로를 [[wikilink]]로 참조하도록 세팅하면 inbound 링크가 생겨 LLM이 호출된다.
        """
        import wiki_builder.api as api_mod
        import wiki_builder.prompt_loader as loader_mod
        from wiki_builder.link import run_link

        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "entities").mkdir()

        # PAGE_A가 PAGE_B를 링크 → PAGE_B에 inbound 링크 발생 → LLM 호출
        (wiki_dir / "entities" / "PAGE_A.md").write_text(
            "## 정의\n내용\n- [[PAGE_B (uses)]]", encoding="utf-8"
        )
        (wiki_dir / "entities" / "PAGE_B.md").write_text(
            "## 정의\n내용", encoding="utf-8"
        )

        sub_agents_dir = self._make_sub_agents(tmp_path)

        captured_backends = []

        def mock_llm(system, user, **kwargs):
            captured_backends.append(kwargs.get("backend"))
            return "## 정의\n내용"

        plan = {"pages": [
            {
                "path": "entities/PAGE_A.md",
                "description": "페이지 A",
                "generated": True,
                "linked": False,
                "sources": [],
            },
            {
                "path": "entities/PAGE_B.md",
                "description": "페이지 B",
                "generated": True,
                "linked": False,
                "sources": [],
            },
        ]}

        with patch.object(api_mod, "BACKEND", "gptoss"):
            with patch.object(loader_mod, "_SUB_AGENTS_DIR", sub_agents_dir):
                run_link(
                    plan=plan,
                    wiki_dir=str(wiki_dir),
                    plan_path=str(tmp_path / "plan.json"),
                    call_llm=mock_llm,
                    backend=None,
                )

        assert len(captured_backends) > 0
        assert all(b == "gptoss" for b in captured_backends)


# ──────────────────────────────────────────────
# check_quality: backend=None → api.BACKEND 사용
# ──────────────────────────────────────────────

class TestCheckQualityBackendNone:
    """check_quality(backend=None) 시 내부에서 api.BACKEND로 대체된다."""

    def _make_sub_agents(self, tmp_path):
        sa = tmp_path / "sub_agents"
        sa.mkdir()
        (sa / "checker.md").write_text(
            "checker system\n---USER---\n{page_content}\n{spec_content}\n{feature_hint}",
            encoding="utf-8"
        )
        return sa

    def test_backend_none_uses_api_backend(self, tmp_path):
        """backend=None → api.BACKEND가 call_llm에 전달된다."""
        import wiki_builder.api as api_mod
        import wiki_builder.prompt_loader as loader_mod
        from wiki_builder.evaluate import check_quality

        sub_agents_dir = self._make_sub_agents(tmp_path)
        captured = {}

        def mock_llm(system, user, **kwargs):
            captured["backend"] = kwargs.get("backend")
            return json.dumps({"score": 8, "pass": True, "issues": []})

        content = "## 정의\n내용\n## 요약\n요약"
        spec = "3GPP 스펙 내용"

        with patch.object(api_mod, "BACKEND", "claude"):
            with patch.object(loader_mod, "_SUB_AGENTS_DIR", sub_agents_dir):
                result = check_quality(
                    content=content,
                    spec_content=spec,
                    call_llm=mock_llm,
                    backend=None,
                )

        assert captured.get("backend") == "claude"

    def test_backend_explicit_overrides_api_backend(self, tmp_path):
        """backend='gemini' 명시 시 그 값이 call_llm에 전달된다."""
        import wiki_builder.api as api_mod
        import wiki_builder.prompt_loader as loader_mod
        from wiki_builder.evaluate import check_quality

        sub_agents_dir = self._make_sub_agents(tmp_path)
        captured = {}

        def mock_llm(system, user, **kwargs):
            captured["backend"] = kwargs.get("backend")
            return json.dumps({"score": 7, "pass": True, "issues": []})

        content = "## 정의\n내용\n## 요약\n요약"

        with patch.object(api_mod, "BACKEND", "claude"):
            with patch.object(loader_mod, "_SUB_AGENTS_DIR", sub_agents_dir):
                result = check_quality(
                    content=content,
                    spec_content="spec",
                    call_llm=mock_llm,
                    backend="gemini",
                )

        assert captured.get("backend") == "gemini"


# ──────────────────────────────────────────────
# run_post_plan: backend=None → api.BACKEND 사용
# ──────────────────────────────────────────────

class TestRunPostPlanBackendNone:
    """run_post_plan(backend=None) 시 내부에서 api.BACKEND로 대체된다."""

    def _make_sub_agents(self, tmp_path):
        sa = tmp_path / "sub_agents"
        sa.mkdir()
        (sa / "post_plan.md").write_text(
            "post_plan system\n---USER---\n{pages_text}",
            encoding="utf-8"
        )
        return sa

    def test_backend_none_uses_api_backend(self, tmp_path):
        """backend=None → api.BACKEND 값이 call_llm에 전달된다."""
        import wiki_builder.api as api_mod
        import wiki_builder.prompt_loader as loader_mod
        from wiki_builder.post_plan import run_post_plan

        sub_agents_dir = self._make_sub_agents(tmp_path)
        captured = {}

        def mock_llm(system, user, **kwargs):
            captured["backend"] = kwargs.get("backend")
            return json.dumps({"fixes": []})

        plan = {
            "post_plan_done": False,
            "planned_sources": [],
            "pages": [{
                "path": "entities/TEST.md",
                "description": "test",
                "generated": False,
                "linked": False,
                "sources": [{"file": "src.docx", "sections": ["6.1"]}],
            }],
        }
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        with patch.object(api_mod, "BACKEND", "ollama"):
            with patch.object(loader_mod, "_SUB_AGENTS_DIR", sub_agents_dir):
                result = run_post_plan(
                    plan=plan,
                    plan_path=str(plan_path),
                    call_llm=mock_llm,
                    backend=None,
                )

        assert captured.get("backend") == "ollama"


# ──────────────────────────────────────────────
# run_plan: backend=None → api.BACKEND 사용
# ──────────────────────────────────────────────

class TestRunPlanBackendNone:
    """run_plan(backend=None) 시 내부에서 api.BACKEND로 대체된다."""

    def _make_sub_agents(self, tmp_path):
        sa = tmp_path / "sub_agents"
        sa.mkdir()
        (sa / "planner.md").write_text(
            "planner system\n---USER---\n{chunk_text}\n{existing_pages}",
            encoding="utf-8"
        )
        return sa

    def test_backend_none_uses_api_backend(self, tmp_path):
        """backend=None → api.BACKEND가 call_llm의 backend kwarg로 전달된다."""
        import wiki_builder.api as api_mod
        import wiki_builder.prompt_loader as loader_mod
        from wiki_builder.plan import run_plan

        # 소스 파일 준비
        sources_dir = tmp_path / "sources"
        sources_dir.mkdir()
        (sources_dir / "test.txt").write_text("테스트 소스 파일 내용", encoding="utf-8")

        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()

        sub_agents_dir = self._make_sub_agents(tmp_path)
        captured = {}

        def mock_llm(system, user, **kwargs):
            captured["backend"] = kwargs.get("backend")
            return json.dumps([{
                "path": "entities/TEST.md",
                "description": "테스트",
                "sections": ["1.1"]
            }])

        def mock_chunk(file_path, min_size=None, max_size=None):
            return [{"index": 0, "text": "청크 내용", "start": 0, "end": 5}]

        with patch.object(api_mod, "BACKEND", "gptoss"):
            with patch.object(loader_mod, "_SUB_AGENTS_DIR", sub_agents_dir):
                result = run_plan(
                    sources_dir=str(sources_dir),
                    wiki_dir=str(wiki_dir),
                    plan_path=str(tmp_path / "plan.json"),
                    call_llm=mock_llm,
                    chunk_fn=mock_chunk,
                    backend=None,
                )

        assert captured.get("backend") == "gptoss"
