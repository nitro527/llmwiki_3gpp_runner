"""
test_link.py — link.py 단위 테스트

테스트 대상:
- _build_link_map(): [[PageName]] 파싱
- _build_link_map(): [[PageName (affects)]] 같은 관계타입 포함 링크도 stem 추출
- run_link(): inbound 링크 없는 페이지 스킵 (LLM 호출 없음)
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


from wiki_builder.link import _build_link_map, run_link


# ──────────────────────────────────────────────
# _build_link_map 테스트
# ──────────────────────────────────────────────

class TestBuildLinkMap:
    def _make_pages(self, paths):
        return [{"path": p, "generated": True, "linked": False} for p in paths]

    def test_simple_wikilink_parsed(self, tmp_path):
        """[[PUSCH]] → PUSCH.md를 가리키는 링크."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()

        (wiki_dir / "entities").mkdir()
        (wiki_dir / "concepts").mkdir()

        (wiki_dir / "entities" / "PUSCH.md").write_text("# PUSCH", encoding="utf-8")
        (wiki_dir / "concepts" / "UCI_Multiplexing.md").write_text(
            "# UCI Multiplexing\n[[PUSCH]]\n", encoding="utf-8"
        )

        pages = self._make_pages(["entities/PUSCH.md", "concepts/UCI_Multiplexing.md"])
        link_map = _build_link_map(str(wiki_dir), pages)

        # UCI_Multiplexing이 PUSCH를 링크함 → PUSCH의 inbound에 UCI_Multiplexing
        assert "entities/PUSCH.md" in link_map
        assert "concepts/UCI_Multiplexing.md" in link_map["entities/PUSCH.md"]

    def test_relation_type_in_link_stripped(self, tmp_path):
        """[[PUSCH (affects)]] → 관계타입 제거 후 PUSCH로 파싱."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()

        (wiki_dir / "entities").mkdir()
        (wiki_dir / "concepts").mkdir()

        (wiki_dir / "entities" / "PUSCH.md").write_text("# PUSCH", encoding="utf-8")
        (wiki_dir / "concepts" / "UCI_Multiplexing.md").write_text(
            "# UCI Multiplexing\n[[PUSCH (affects)]]\n", encoding="utf-8"
        )

        pages = self._make_pages(["entities/PUSCH.md", "concepts/UCI_Multiplexing.md"])
        link_map = _build_link_map(str(wiki_dir), pages)

        assert "entities/PUSCH.md" in link_map
        assert "concepts/UCI_Multiplexing.md" in link_map["entities/PUSCH.md"]

    def test_multiple_links_in_one_page(self, tmp_path):
        """페이지 하나가 여러 링크를 가질 수 있다."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()

        (wiki_dir / "entities").mkdir()

        (wiki_dir / "entities" / "A.md").write_text("# A", encoding="utf-8")
        (wiki_dir / "entities" / "B.md").write_text("# B", encoding="utf-8")
        (wiki_dir / "entities" / "C.md").write_text(
            "# C\n[[A]]\n[[B (related)]]\n", encoding="utf-8"
        )

        pages = self._make_pages(["entities/A.md", "entities/B.md", "entities/C.md"])
        link_map = _build_link_map(str(wiki_dir), pages)

        assert "entities/C.md" in link_map.get("entities/A.md", set())
        assert "entities/C.md" in link_map.get("entities/B.md", set())

    def test_nonexistent_file_skipped(self, tmp_path):
        """파일이 없는 페이지는 스킵 — 오류 없이 처리."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "entities").mkdir()

        pages = self._make_pages(["entities/PUSCH.md"])  # 파일 없음
        link_map = _build_link_map(str(wiki_dir), pages)  # 오류 없이 완료

        assert "entities/PUSCH.md" not in link_map

    def test_unknown_link_target_ignored(self, tmp_path):
        """존재하지 않는 페이지 링크([[UnknownPage]])는 map에 추가되지 않음."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "entities").mkdir()

        (wiki_dir / "entities" / "PUSCH.md").write_text(
            "# PUSCH\n[[UnknownPage]]\n", encoding="utf-8"
        )

        pages = self._make_pages(["entities/PUSCH.md"])
        link_map = _build_link_map(str(wiki_dir), pages)

        # UnknownPage는 pages 목록에 없으므로 map에 없어야 함
        assert "concepts/UnknownPage.md" not in link_map
        assert "entities/UnknownPage.md" not in link_map

    def test_self_link_not_in_inbound(self, tmp_path):
        """자기 자신을 링크하는 경우 inbound에 자신이 들어가지만 오류 없음."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "entities").mkdir()

        (wiki_dir / "entities" / "PUSCH.md").write_text(
            "# PUSCH\n[[PUSCH]]\n", encoding="utf-8"
        )

        pages = self._make_pages(["entities/PUSCH.md"])
        link_map = _build_link_map(str(wiki_dir), pages)
        # 자기 자신 링크도 오류 없이 처리
        assert isinstance(link_map, dict)


# ──────────────────────────────────────────────
# run_link 테스트
# ──────────────────────────────────────────────

class TestRunLink:
    def _make_plan(self, pages):
        return {"pages": pages}

    def _make_page(self, path, generated=True, linked=False):
        return {"path": path, "generated": generated, "linked": linked, "sources": []}

    def test_no_inbound_link_skips_llm(self, tmp_path):
        """inbound 링크가 없는 페이지는 LLM 호출 없이 linked=True로 마킹."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "entities").mkdir()

        (wiki_dir / "entities" / "PUSCH.md").write_text("# PUSCH 내용", encoding="utf-8")

        plan_path = tmp_path / "plan.json"
        plan = self._make_plan([self._make_page("entities/PUSCH.md")])

        call_count = [0]

        def mock_llm(system, user, **kwargs):
            call_count[0] += 1
            return "updated content"

        run_link(plan, str(wiki_dir), str(plan_path), mock_llm, backend="claude")

        assert call_count[0] == 0
        assert plan["pages"][0]["linked"] is True

    def test_inbound_link_triggers_llm(self, tmp_path):
        """inbound 링크가 있는 페이지는 LLM을 호출한다."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "entities").mkdir()
        (wiki_dir / "concepts").mkdir()

        (wiki_dir / "entities" / "PUSCH.md").write_text("# PUSCH 내용", encoding="utf-8")
        (wiki_dir / "concepts" / "UCI_Multiplexing.md").write_text(
            "# UCI\n[[PUSCH]]\n", encoding="utf-8"
        )

        plan_path = tmp_path / "plan.json"
        plan = self._make_plan([
            self._make_page("entities/PUSCH.md"),
            self._make_page("concepts/UCI_Multiplexing.md"),
        ])

        call_count = [0]

        def mock_llm(system, user, **kwargs):
            call_count[0] += 1
            return "updated content"

        run_link(plan, str(wiki_dir), str(plan_path), mock_llm, backend="claude")

        # PUSCH는 inbound 링크(UCI_Multiplexing)가 있으므로 LLM 호출
        assert call_count[0] >= 1

    def test_already_linked_page_skipped(self, tmp_path):
        """linked=True인 페이지는 스킵."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "entities").mkdir()

        (wiki_dir / "entities" / "PUSCH.md").write_text("# PUSCH", encoding="utf-8")

        plan_path = tmp_path / "plan.json"
        page = self._make_page("entities/PUSCH.md", linked=True)
        plan = self._make_plan([page])

        call_count = [0]

        def mock_llm(system, user, **kwargs):
            call_count[0] += 1
            return "updated"

        run_link(plan, str(wiki_dir), str(plan_path), mock_llm)

        assert call_count[0] == 0

    def test_plan_json_saved_after_link(self, tmp_path):
        """run_link 완료 후 plan.json이 저장된다."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "entities").mkdir()

        (wiki_dir / "entities" / "PUSCH.md").write_text("# PUSCH", encoding="utf-8")

        plan_path = tmp_path / "plan.json"
        plan = self._make_plan([self._make_page("entities/PUSCH.md")])

        run_link(plan, str(wiki_dir), str(plan_path), lambda s, u, **kw: "ok")

        assert plan_path.exists()
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        assert "pages" in data
