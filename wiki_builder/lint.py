"""
lint.py — Phase 6: Lint

wiki 건강 검진.
- Python 직접 계산: 고아 페이지, 역방향 링크 누락, 존재하지 않는 [[링크]]
- LLM 호출: 내용 모순, 오래된 주장, 데이터 공백 (5페이지 묶음 단위)
"""

import json
import logging
import re
from datetime import date
from pathlib import Path

from wiki_builder.prompts import LINT_SYSTEM, LINT_USER

logger = logging.getLogger(__name__)

BATCH_SIZE = 5       # LLM에 한 번에 넘길 페이지 수
MAX_PAGE_CHARS = 5000  # 페이지 1개 최대


def run_lint(wiki_dir: str, call_llm) -> dict:
    """
    wiki 전체 건강 검진.

    Returns:
        {
            "orphan_pages": [str],
            "missing_backlinks": [{"page": str, "missing_from": str}],
            "broken_links": [{"page": str, "link": str}],
            "contradictions": [...],
            "stale_claims": [...],
            "data_gaps": [...],
            "report_path": str
        }
    """
    wiki_path = Path(wiki_dir)
    pages = _collect_pages(wiki_path)

    if not pages:
        logger.warning("wiki 페이지 없음 — lint 스킵")
        return {"error": "wiki 페이지 없음"}

    logger.info(f"Lint 시작: {len(pages)}개 페이지")

    # ── Python 직접 계산 ──
    link_map = _build_link_map(pages, wiki_path)
    orphan_pages = _find_orphans(pages, link_map)
    missing_backlinks = _find_missing_backlinks(pages, link_map, wiki_path)
    broken_links = _find_broken_links(pages, wiki_path)

    logger.info(f"고아 페이지: {len(orphan_pages)}개, 역링크 누락: {len(missing_backlinks)}개, "
                f"깨진 링크: {len(broken_links)}개")

    # ── LLM 호출 (배치) ──
    contradictions, stale_claims, data_gaps = [], [], []
    batches = [pages[i:i+BATCH_SIZE] for i in range(0, len(pages), BATCH_SIZE)]
    for i, batch in enumerate(batches):
        logger.info(f"LLM 분석 배치 {i+1}/{len(batches)}")
        result = _llm_analyze_batch(batch, wiki_path, call_llm)
        contradictions.extend(result.get("contradictions", []))
        stale_claims.extend(result.get("stale_claims", []))
        data_gaps.extend(result.get("data_gaps", []))

    report = {
        "orphan_pages": orphan_pages,
        "missing_backlinks": missing_backlinks,
        "broken_links": broken_links,
        "contradictions": contradictions,
        "stale_claims": stale_claims,
        "data_gaps": data_gaps,
    }

    report_path = _save_report(report, wiki_path)
    report["report_path"] = report_path

    _append_log(wiki_path, f"lint | 고아:{len(orphan_pages)} 역링크누락:{len(missing_backlinks)} "
                           f"모순:{len(contradictions)} 공백:{len(data_gaps)}")

    logger.info(f"Lint 완료 — 리포트: {report_path}")
    return report


def _collect_pages(wiki_path: Path) -> list[str]:
    """wiki의 모든 .md 페이지 경로 수집 (index.md, log.md 제외)."""
    pages = []
    for subdir in ["entities", "concepts", "internal", "query"]:
        d = wiki_path / subdir
        if d.exists():
            for md in d.glob("*.md"):
                pages.append(f"{subdir}/{md.name}")
    return sorted(pages)


def _build_link_map(pages: list[str], wiki_path: Path) -> dict[str, list[str]]:
    """각 페이지가 링크하는 페이지 목록 빌드. {page: [linked_page, ...]}"""
    link_map = {}
    for page in pages:
        content = (wiki_path / page).read_text(encoding="utf-8", errors="ignore")
        links = re.findall(r'\[\[([^\]|#]+?)(?:\|[^\]]*)?\]\]', content)
        # 링크 이름 → 실제 파일 경로로 변환
        resolved = []
        for link in links:
            resolved_path = _resolve_link(link, pages)
            if resolved_path:
                resolved.append(resolved_path)
        link_map[page] = resolved
    return link_map


def _resolve_link(link_name: str, pages: list[str]) -> str | None:
    """[[링크이름]] → 실제 파일 경로. 못 찾으면 None."""
    stem = link_name.strip()
    for page in pages:
        if Path(page).stem == stem:
            return page
    return None


def _find_orphans(pages: list[str], link_map: dict) -> list[str]:
    """inbound 링크가 없는 페이지 (단, query/ 제외)."""
    inbound = {p: 0 for p in pages}
    for linked_list in link_map.values():
        for target in linked_list:
            if target in inbound:
                inbound[target] += 1
    return [p for p, count in inbound.items()
            if count == 0 and not p.startswith("query/")]


def _find_missing_backlinks(pages: list[str], link_map: dict, wiki_path: Path) -> list[dict]:
    """A→B 링크가 있는데 B→A 역링크가 없는 경우."""
    missing = []
    for page_a, linked in link_map.items():
        for page_b in linked:
            if page_b not in link_map:
                continue
            if page_a not in link_map.get(page_b, []):
                missing.append({"page": page_b, "missing_from": page_a})
    return missing


def _find_broken_links(pages: list[str], wiki_path: Path) -> list[dict]:
    """[[링크]] 참조가 있는데 해당 파일이 없는 경우."""
    all_stems = {Path(p).stem for p in pages}
    broken = []
    for page in pages:
        content = (wiki_path / page).read_text(encoding="utf-8", errors="ignore")
        links = re.findall(r'\[\[([^\]|#]+?)(?:\|[^\]]*)?\]\]', content)
        for link in links:
            stem = link.strip()
            if stem not in all_stems:
                broken.append({"page": page, "link": stem})
    return broken


def _llm_analyze_batch(batch: list[str], wiki_path: Path, call_llm) -> dict:
    """LLM으로 배치 내 모순/오래된주장/공백 분석."""
    parts = []
    for page in batch:
        content = (wiki_path / page).read_text(encoding="utf-8", errors="ignore")
        if len(content) > MAX_PAGE_CHARS:
            content = content[:MAX_PAGE_CHARS] + "\n...(truncated)"
        parts.append(f"## [{page}]\n{content}")

    pages_content = "\n\n".join(parts)
    user_msg = LINT_USER.format(pages_content=pages_content)
    raw = call_llm(LINT_SYSTEM, user_msg, temperature=0.1, json_format=True)

    if raw.startswith("[LLM 호출 실패]"):
        logger.warning(f"Lint LLM 호출 실패: {raw}")
        return {}

    return _parse_lint_json(raw)


def _parse_lint_json(text: str) -> dict:
    """LLM 응답에서 JSON 파싱."""
    m = re.search(r'```json\s*([\s\S]+?)\s*```', text)
    if m:
        text = m.group(1)
    try:
        return json.loads(text.strip())
    except Exception:
        logger.warning("Lint JSON 파싱 실패")
        return {}


def _save_report(report: dict, wiki_path: Path) -> str:
    """lint 리포트를 wiki/lint_YYYY-MM-DD.md로 저장."""
    today = date.today().strftime("%Y-%m-%d")
    report_path = wiki_path / f"lint_{today}.md"

    lines = [f"# Lint 리포트 — {today}\n"]

    lines.append(f"## 요약\n")
    lines.append(f"| 항목 | 건수 |\n|------|------|\n")
    lines.append(f"| 고아 페이지 | {len(report['orphan_pages'])} |\n")
    lines.append(f"| 역방향 링크 누락 | {len(report['missing_backlinks'])} |\n")
    lines.append(f"| 깨진 링크 | {len(report['broken_links'])} |\n")
    lines.append(f"| 내용 모순 | {len(report['contradictions'])} |\n")
    lines.append(f"| 오래된 주장 | {len(report['stale_claims'])} |\n")
    lines.append(f"| 데이터 공백 | {len(report['data_gaps'])} |\n\n")

    if report["orphan_pages"]:
        lines.append("## 고아 페이지 (inbound 링크 없음)\n")
        for p in report["orphan_pages"]:
            lines.append(f"- {p}\n")
        lines.append("\n")

    if report["broken_links"]:
        lines.append("## 깨진 링크\n")
        for item in report["broken_links"]:
            lines.append(f"- `{item['page']}` → [[{item['link']}]] (파일 없음)\n")
        lines.append("\n")

    if report["missing_backlinks"]:
        lines.append("## 역방향 링크 누락\n")
        for item in report["missing_backlinks"]:
            lines.append(f"- `{item['page']}`에 `{item['missing_from']}` 역링크 없음\n")
        lines.append("\n")

    if report["contradictions"]:
        lines.append("## 내용 모순\n")
        for item in report["contradictions"]:
            pages = ", ".join(item.get("pages", []))
            lines.append(f"- [{pages}] {item.get('issue', '')}\n")
        lines.append("\n")

    if report["stale_claims"]:
        lines.append("## 오래된 주장\n")
        for item in report["stale_claims"]:
            lines.append(f"- `{item.get('page', '')}`: {item.get('issue', '')}\n")
        lines.append("\n")

    if report["data_gaps"]:
        lines.append("## 데이터 공백 (조사 권장)\n")
        for item in report["data_gaps"]:
            lines.append(f"- **{item.get('topic', '')}**: {item.get('suggestion', '')}\n")
        lines.append("\n")

    report_path.write_text("".join(lines), encoding="utf-8")
    return str(report_path.relative_to(wiki_path))


def _append_log(wiki_path: Path, entry: str) -> None:
    log_path = wiki_path / "log.md"
    today = date.today().strftime("%Y-%m-%d")
    line = f"## [{today}] {entry}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)
