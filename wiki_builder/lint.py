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

from wiki_builder.prompt_loader import load_prompt
from wiki_builder.utils import save_plan, extract_json_from_llm

LINT_SYSTEM, LINT_USER = load_prompt("lint")

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
    missing_backlinks = _find_missing_backlinks(pages, link_map)
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


def _find_missing_backlinks(pages: list[str], link_map: dict) -> list[dict]:
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

    result = extract_json_from_llm(raw)
    if result is None:
        logger.warning("Lint JSON 파싱 실패")
        return {}
    return result if isinstance(result, dict) else {}


def _save_report(report: dict, wiki_path: Path) -> str:
    """lint 리포트를 wiki/lint_YYYY-MM-DD.md로 저장."""
    today = date.today().strftime("%Y-%m-%d")
    report_path = wiki_path / f"lint_{today}.md"

    sections = [f"# Lint 리포트 — {today}\n"]

    # 요약 표
    counts = {
        "고아 페이지":       len(report["orphan_pages"]),
        "역방향 링크 누락":  len(report["missing_backlinks"]),
        "깨진 링크":         len(report["broken_links"]),
        "내용 모순":         len(report["contradictions"]),
        "오래된 주장":       len(report["stale_claims"]),
        "데이터 공백":       len(report["data_gaps"]),
    }
    rows = "\n".join(f"| {label} | {cnt} |" for label, cnt in counts.items())
    sections.append(f"\n## 요약\n| 항목 | 건수 |\n|------|------|\n{rows}\n")

    if report["orphan_pages"]:
        items = "\n".join(f"- {p}" for p in report["orphan_pages"])
        sections.append(f"\n## 고아 페이지 (inbound 링크 없음)\n{items}\n")

    if report["broken_links"]:
        items = "\n".join(
            f"- `{item['page']}` → [[{item['link']}]] (파일 없음)"
            for item in report["broken_links"]
        )
        sections.append(f"\n## 깨진 링크\n{items}\n")

    if report["missing_backlinks"]:
        items = "\n".join(
            f"- `{item['page']}`에 `{item['missing_from']}` 역링크 없음"
            for item in report["missing_backlinks"]
        )
        sections.append(f"\n## 역방향 링크 누락\n{items}\n")

    if report["contradictions"]:
        items = "\n".join(
            f"- [{', '.join(item.get('pages', []))}] {item.get('issue', '')}"
            for item in report["contradictions"]
        )
        sections.append(f"\n## 내용 모순\n{items}\n")

    if report["stale_claims"]:
        items = "\n".join(
            f"- `{item.get('page', '')}`: {item.get('issue', '')}"
            for item in report["stale_claims"]
        )
        sections.append(f"\n## 오래된 주장\n{items}\n")

    if report["data_gaps"]:
        items = "\n".join(
            f"- **{item.get('topic', '')}**: {item.get('suggestion', '')}"
            for item in report["data_gaps"]
        )
        sections.append(f"\n## 데이터 공백 (조사 권장)\n{items}\n")

    report_path.write_text("\n".join(sections), encoding="utf-8")
    return str(report_path.relative_to(wiki_path))


def _append_log(wiki_path: Path, entry: str) -> None:
    log_path = wiki_path / "log.md"
    today = date.today().strftime("%Y-%m-%d")
    line = f"## [{today}] {entry}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


# ──────────────────────────────────────────────
# Post-Lint: 이슈별 후속 조치
# ──────────────────────────────────────────────

def run_post_lint(
    report: dict,
    plan: dict,
    plan_path: str,
) -> dict:
    """
    Lint 리포트를 사용자에게 출력하고, 이슈별로 후속 조치를 수행할지 확인.

    Returns:
        {
            "needs_generate": bool,
            "needs_link": bool,
            "added_pages": [str],    # plan에 신규 추가된 페이지 경로
            "reset_pages": [str],    # generated=False 리셋된 페이지
            "relink_pages": [str],   # linked=False 리셋된 페이지
        }
    """
    result = {
        "needs_generate": False,
        "needs_link": False,
        "added_pages": [],
        "reset_pages": [],
        "relink_pages": [],
    }

    _print_report_summary(report)

    _handle_broken_links(report.get("broken_links", []), plan, plan_path, result)
    _handle_missing_backlinks(report.get("missing_backlinks", []), plan, plan_path, result)
    _handle_contradictions(report.get("contradictions", []), plan, plan_path, result)
    _print_info_items(report.get("orphan_pages", []), report.get("stale_claims", []))

    print(f"\n{'='*60}")
    _print_action_summary(result)

    return result


def _handle_broken_links(
    broken: list[dict],
    plan: dict,
    plan_path: str,
    result: dict,
) -> None:
    """깨진 링크 이슈 처리: 신규 페이지를 plan에 추가할지 사용자에게 확인."""
    if not broken:
        return

    print(f"\n{'='*60}")
    print(f"[1/3] 깨진 링크 {len(broken)}개 - 대상 파일이 없습니다.")
    existing_paths = {p["path"] for p in plan.get("pages", [])}
    candidates = _collect_broken_candidates(broken, existing_paths)

    if not candidates:
        print("  (추가할 신규 페이지 없음 - 이미 plan에 있거나 중복)")
        return

    for c in candidates:
        print(f"  + {c['path']}  (링크 출처: {c['from_page']})")

    if not _ask_user(f"\n이 {len(candidates)}개 페이지를 plan에 추가하고 생성하시겠습니까?\n  [y/N]: "):
        print("  → 스킵.")
        return

    for c in candidates:
        plan["pages"].append({
            "path": c["path"],
            "description": c["description"],
            "generated": False,
            "linked": False,
            "sources": [],
        })
        result["added_pages"].append(c["path"])
    _save_plan(plan, plan_path)
    result["needs_generate"] = True
    result["needs_link"] = True
    print(f"  → {len(candidates)}개 plan에 추가됨.")


def _handle_missing_backlinks(
    backlinks: list[dict],
    plan: dict,
    plan_path: str,
    result: dict,
) -> None:
    """역방향 링크 누락 처리: 해당 페이지의 linked 플래그를 리셋할지 확인."""
    if not backlinks:
        return

    print(f"\n{'='*60}")
    print(f"[2/3] 역방향 링크 누락 {len(backlinks)}개")
    pages_to_relink = list({item["page"] for item in backlinks})
    for p in pages_to_relink[:10]:
        print(f"  - {p}")
    if len(pages_to_relink) > 10:
        print(f"  ... 외 {len(pages_to_relink)-10}개")

    if not _ask_user(
        f"\n이 {len(pages_to_relink)}개 페이지의 linked 플래그를 리셋하고 링크 단계를 재실행하시겠습니까?\n  [y/N]: "
    ):
        print("  → 스킵.")
        return

    relink_set = set(pages_to_relink)
    for page in plan.get("pages", []):
        if page["path"] in relink_set:
            page["linked"] = False
            result["relink_pages"].append(page["path"])
    _save_plan(plan, plan_path)
    result["needs_link"] = True
    print(f"  → {len(result['relink_pages'])}개 linked 리셋됨.")


def _handle_contradictions(
    contradictions: list[dict],
    plan: dict,
    plan_path: str,
    result: dict,
) -> None:
    """내용 모순 처리: 해당 페이지를 재생성(generated 리셋)할지 확인."""
    if not contradictions:
        return

    print(f"\n{'='*60}")
    print(f"[3/3] 내용 모순 {len(contradictions)}개")

    # 모순 관련 페이지 목록 (중복 제거, 순서 유지)
    seen: set[str] = set()
    contra_pages: list[str] = []
    for item in contradictions:
        for p in item.get("pages", []):
            if p not in seen:
                seen.add(p)
                contra_pages.append(p)

    for p in contra_pages:
        print(f"  - {p}")
        for item in contradictions:
            if p in item.get("pages", []):
                print(f"      모순: {item.get('issue', '')}")
                break

    if not _ask_user(
        f"\n이 {len(contra_pages)}개 페이지를 재생성하시겠습니까? (generated 플래그 리셋)\n  [y/N]: "
    ):
        print("  → 스킵.")
        return

    contra_set = set(contra_pages)
    for page in plan.get("pages", []):
        if page["path"] in contra_set:
            page["generated"] = False
            page["linked"] = False
            result["reset_pages"].append(page["path"])
    _save_plan(plan, plan_path)
    result["needs_generate"] = True
    result["needs_link"] = True
    print(f"  → {len(result['reset_pages'])}개 재생성 대기 등록됨.")


def _print_info_items(orphans: list[str], stale: list[dict]) -> None:
    """고아 페이지 / 오래된 주장 — 사용자에게 보고만, 조치 없음."""
    if orphans:
        print(f"\n{'='*60}")
        print(f"[참고] 고아 페이지 {len(orphans)}개 (inbound 링크 없음 - 수동 확인 권장)")
        for p in orphans[:5]:
            print(f"  - {p}")
        if len(orphans) > 5:
            print(f"  ... 외 {len(orphans)-5}개")

    if stale:
        print(f"\n[참고] 오래된 주장 {len(stale)}개 (수동 검토 권장)")
        for item in stale[:3]:
            print(f"  - {item.get('page','')}: {item.get('issue','')}")
        if len(stale) > 3:
            print(f"  ... 외 {len(stale)-3}개")


def _collect_broken_candidates(
    broken: list[dict],
    existing_paths: set,
) -> list[dict]:
    """깨진 링크에서 plan에 추가할 신규 페이지 후보 수집."""
    seen = set()
    candidates = []
    for item in broken:
        stem = item["link"].strip()
        path = _infer_path(stem, existing_paths)
        if path in existing_paths or path in seen:
            continue
        seen.add(path)
        candidates.append({
            "path": path,
            "description": f"{stem} (링크에서 자동 추가됨)",
            "from_page": item["page"],
        })
    return candidates


def _infer_path(stem: str, existing_paths: set) -> str:
    """링크 이름 → wiki 파일 경로 추론. 대문자 약어 → entities/, 그 외 → concepts/."""
    clean = stem.replace("-", "").replace("_", "")
    if clean.isupper() and len(clean) >= 2:
        return f"entities/{stem}.md"
    return f"concepts/{stem}.md"


def _ask_user(prompt: str) -> bool:
    """프롬프트를 출력하고 y/N 입력을 받아 bool 반환. 비대화형 환경에서는 False."""
    try:
        return input(prompt).strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def _save_plan(plan: dict, plan_path: str) -> None:
    save_plan(plan, plan_path)


def _print_report_summary(report: dict) -> None:
    print(f"\n{'='*60}")
    print("[Lint 결과 요약]")
    print(f"  고아 페이지         : {len(report.get('orphan_pages', []))}개")
    print(f"  깨진 링크           : {len(report.get('broken_links', []))}개")
    print(f"  역방향 링크 누락    : {len(report.get('missing_backlinks', []))}개")
    print(f"  내용 모순           : {len(report.get('contradictions', []))}개")
    print(f"  오래된 주장         : {len(report.get('stale_claims', []))}개")
    print(f"  데이터 공백         : {len(report.get('data_gaps', []))}개")
    print(f"{'='*60}")


def _print_action_summary(result: dict) -> None:
    print("[후속 조치 요약]")
    if result["added_pages"]:
        print(f"  신규 페이지 추가  : {len(result['added_pages'])}개")
    if result["reset_pages"]:
        print(f"  재생성 예약       : {len(result['reset_pages'])}개")
    if result["relink_pages"]:
        print(f"  재링크 예약       : {len(result['relink_pages'])}개")
    if not any([result["added_pages"], result["reset_pages"], result["relink_pages"]]):
        print("  조치 없음.")
    if result["needs_generate"]:
        print("  → run_generate 실행 예정")
    if result["needs_link"]:
        print("  → run_link 실행 예정")
