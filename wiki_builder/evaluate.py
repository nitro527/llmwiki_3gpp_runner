"""
evaluate.py — Quality Checker + Phase 4 Evaluator

check_quality(content, spec_content, call_llm) -> dict
    품질 평가. 8점 만점, 7점 이상 pass.

run_evaluate(plan, wiki_dir, ...) -> None
    불합격 페이지 분석 → 프롬프트 개선 → 재실행 (최대 5회)
    매 라운드 변경 전/후를 eval_history.json에 기록.
"""

import hashlib
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

PASS_SCORE = 7
MAX_EVAL_ROUNDS = 5


# ──────────────────────────────────────────────
# Quality Checker
# ──────────────────────────────────────────────

def check_quality(
    content: str,
    spec_content: str,
    call_llm,
    *,
    backend: str = "claude",
) -> dict:
    """
    wiki 페이지 품질 평가.

    Returns:
        {"score": int, "pass": bool, "issues": list, "details": dict}
    """
    from wiki_builder.prompts import CHECKER_SYSTEM, CHECKER_USER

    quick = _quick_check(content)

    user_msg = CHECKER_USER.format(
        page_content=content[:4000],
        spec_content=spec_content[:2000],
    )

    for attempt in range(3):
        raw = call_llm(CHECKER_SYSTEM, user_msg, temperature=0.1, backend=backend, json_format=True)

        if raw.startswith("[LLM 호출 실패]"):
            logger.error(f"Checker LLM 실패: {raw}")
            return quick

        result = _parse_checker_response(raw)
        if result is not None:
            return result

        logger.warning(f"Checker 파싱 실패 (시도 {attempt + 1}/3)")

    logger.error("Checker LLM 파싱 3회 실패 — 구조 검사 결과 사용")
    return quick


def _quick_check(content: str) -> dict:
    """구조 기반 빠른 품질 검사 (LLM 없이)."""
    required_sections = [
        "## 정의", "## 요약", "## 상세 설명",
        "## 인과 관계", "## 관련 개념", "## 스펙 근거", "## 소스",
    ]
    missing = [s for s in required_sections if s not in content]
    structure_score = 2 if not missing else (1 if len(missing) <= 2 else 0)

    related_section = re.search(r'## 관련 개념\n(.*?)(?=\n##|\Z)', content, re.DOTALL)
    has_relation_types = False
    if related_section:
        links = re.findall(r'\[\[([^\]]+)\]\]', related_section.group(1))
        has_relation_types = all(
            re.search(r'\([^)]+\)', link) or '(' in related_section.group(1)
            for link in links
        ) if links else True

    has_bold = bool(re.search(r'\*\*[^*]+\*\*', content))

    score = structure_score
    score += 1 if not has_bold else 0
    score += 1 if has_relation_types else 0
    score += 1 if "## 스펙 근거" in content else 0
    score += 2  # hallucination: 구조로 판단 불가, 기본 2점
    score += 1 if "## 상세 설명" in content else 0

    issues = []
    if missing:
        issues.append(f"누락 섹션: {', '.join(missing)}")
    if has_bold:
        issues.append("**bold** 사용 감지 — [[wikilink]] 로 변경 필요")
    if not has_relation_types:
        issues.append("관련 개념에 관계 타입 누락")

    return {
        "score": score,
        "pass": score >= PASS_SCORE,
        "issues": issues,
        "details": {
            "structure": structure_score,
            "no_translation": 1 if not has_bold else 0,
            "relation_types": 1 if has_relation_types else 0,
            "source_reference": 1 if "## 스펙 근거" in content else 0,
            "no_hallucination": 2,
            "spec_based": 1 if "## 상세 설명" in content else 0,
        },
        "method": "quick_check",
    }


def _parse_checker_response(raw: str) -> dict | None:
    text = re.sub(r'```json\s*', '', raw)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()

    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        return None

    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return None

    score = data.get("score", 0)
    return {
        "score": score,
        "pass": data.get("pass", score >= PASS_SCORE),
        "issues": data.get("issues", []),
        "details": data.get("details", {}),
        "method": "llm_check",
    }


# ──────────────────────────────────────────────
# Phase 4: Evaluator
# ──────────────────────────────────────────────

def run_evaluate(
    plan: dict,
    wiki_dir: str,
    plan_path: str,
    eval_log: str,
    call_llm,
    extract_spec_fn,
    *,
    backend: str = "claude",
    initial_failed: list[dict] | None = None,
) -> None:
    """
    Phase 4 실행: 불합격 페이지 분석 및 프롬프트 개선.

    initial_failed: generate phase에서 직접 넘어온 불합격 목록.
                    None이면 generated=True 페이지를 재평가하여 수집.
    """
    import wiki_builder.prompts as prompts_module
    from wiki_builder.prompts import EVALUATOR_SYSTEM, EVALUATOR_USER

    pages = plan.get("pages", [])
    history_path = Path(eval_log).parent / "eval_history.json"
    history = EvalHistory(history_path)
    session = history.new_session()

    # 불합격 페이지 수집
    # - generate에서 바로 넘어온 경우: initial_failed 사용
    # - --phase evaluate 단독 실행: generated=True 페이지 재평가
    if initial_failed is not None:
        from_generate = [
            {"path": fp["path"], "score": fp.get("score"), "issues": fp.get("issues", [])}
            for fp in initial_failed
        ]
        from_existing = _collect_failed_pages(pages, wiki_dir, extract_spec_fn, call_llm, backend)
        # 중복 제거 (generate 불합격은 파일이 없으므로 from_existing에 안 들어오지만 방어적으로)
        existing_paths = {fp["path"] for fp in from_existing}
        merged = from_existing + [fp for fp in from_generate if fp["path"] not in existing_paths]
        failed_pages = merged
        logger.info(f"Generate 불합격 {len(from_generate)}개 + 기존 생성 불합격 {len(from_existing)}개 = 총 {len(failed_pages)}개")
    else:
        failed_pages = _collect_failed_pages(pages, wiki_dir, extract_spec_fn, call_llm, backend)

    if not failed_pages:
        logger.info("불합격 페이지 없음 — Evaluate 스킵")
        history.close_session(session, note="불합격 없음")
        history.save()
        return

    logger.info(f"불합격 페이지 {len(failed_pages)}개 발견")

    for round_idx in range(MAX_EVAL_ROUNDS):
        logger.info(f"Evaluate 라운드 {round_idx + 1}/{MAX_EVAL_ROUNDS}")

        # ── Before 스냅샷 ──
        before_snapshot = _snapshot_pages(failed_pages, prompts_module)

        failed_summary = _format_failed_summary(failed_pages)
        user_msg = EVALUATOR_USER.format(
            failed_pages=failed_summary,
            current_prompt=prompts_module.GENERATOR_SYSTEM,
            current_user_prompt=prompts_module.GENERATOR_USER,
        )

        raw = call_llm(EVALUATOR_SYSTEM, user_msg, temperature=0.1, backend=backend, json_format=True)

        if raw.startswith("[LLM 호출 실패]"):
            logger.error(f"Evaluator LLM 실패: {raw}")
            history.add_round(session, before_snapshot, change={"error": raw}, after=None)
            history.save()
            break

        analysis = _parse_evaluator_response(raw)
        if analysis is None:
            logger.error("Evaluator 응답 파싱 실패")
            history.add_round(session, before_snapshot, change={"error": "파싱 실패"}, after=None)
            history.save()
            break

        # ── 사람 컨펌 대기 ──
        _print_analysis(round_idx + 1, analysis, failed_pages)
        try:
            answer = input("계속하려면 [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"

        change_record = {
            "root_cause": analysis.get("root_cause", ""),
            "affected_pages": analysis.get("affected_pages", []),
            "prompt_fix": analysis.get("prompt_fix", {}),
            "confidence": analysis.get("confidence", ""),
            "user_confirmed": answer == "y",
        }

        if answer != "y":
            logger.info("사용자가 재실행 거부 — Evaluate 종료")
            history.add_round(session, before_snapshot, change=change_record, after=None)
            history.save()
            _write_eval_log(eval_log, round_idx + 1, failed_pages, analysis)
            break

        # ── 재생성 ──
        failed_paths = {fp["path"] for fp in failed_pages}
        for page in pages:
            if page["path"] in failed_paths:
                page["generated"] = False

        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

        from wiki_builder.generate import run_generate
        newly_failed = run_generate(
            plan=plan,
            wiki_dir=wiki_dir,
            plan_path=plan_path,
            call_llm=call_llm,
            extract_spec_fn=extract_spec_fn,
            check_quality_fn=check_quality,
            backend=backend,
        )

        # ── After 스냅샷 + 델타 계산 ──
        newly_failed_paths = {fp["path"] for fp in newly_failed}
        after_records = _build_after_records(
            failed_pages, newly_failed_paths, wiki_dir, extract_spec_fn, call_llm, backend
        )
        after_snapshot = {
            "results": after_records,
            "improved_count": sum(1 for r in after_records if r["passed"]),
            "still_failed_count": sum(1 for r in after_records if not r["passed"]),
        }

        history.add_round(session, before_snapshot, change=change_record, after=after_snapshot)
        _write_eval_log(eval_log, round_idx + 1, failed_pages, analysis)
        history.save()

        # ── 결과 출력 ──
        _print_round_result(after_records)

        if not newly_failed:
            logger.info("모든 페이지 합격!")
            break

        failed_pages = newly_failed
        logger.info(f"여전히 불합격: {len(failed_pages)}개")

    else:
        logger.warning(f"최대 {MAX_EVAL_ROUNDS}회 반복 후에도 개선 없음 — 사람 개입 필요")
        print(f"\n[경고] {MAX_EVAL_ROUNDS}회 반복 후에도 개선되지 않은 페이지:")
        for fp in failed_pages:
            print(f"  - {fp['path']}")

    # ── Session 요약 ──
    history.close_session(session)
    history.save()
    _print_session_summary(history, session)


# ──────────────────────────────────────────────
# EvalHistory — eval_history.json 관리
# ──────────────────────────────────────────────

class EvalHistory:
    """
    eval_history.json 구조:
    {
      "sessions": [
        {
          "id": "2026-05-02T10:00:00",
          "rounds": [...],
          "summary": {...}
        }
      ]
    }
    """

    def __init__(self, path: Path):
        self._path = path
        self._data = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("eval_history.json 로드 실패 — 새로 시작")
        return {"sessions": []}

    def save(self) -> None:
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def new_session(self) -> str:
        """새 session 생성. session id(타임스탬프 문자열) 반환."""
        session_id = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._data["sessions"].append({
            "id": session_id,
            "rounds": [],
            "summary": None,
        })
        return session_id

    def add_round(self, session_id: str, before: dict, change: dict, after: dict | None) -> None:
        """라운드 기록 추가."""
        session = self._find_session(session_id)
        if session is None:
            return

        round_num = len(session["rounds"]) + 1
        record = {
            "round": round_num,
            "timestamp": datetime.now().isoformat(),
            "before": before,
            "change": change,
            "after": after,
            "delta": _compute_delta(before, after) if after else None,
        }
        session["rounds"].append(record)

    def close_session(self, session_id: str, note: str = "") -> None:
        """session 요약 계산 후 닫기."""
        session = self._find_session(session_id)
        if session is None:
            return

        rounds = session["rounds"]
        if not rounds:
            session["summary"] = {"note": note or "라운드 없음"}
            return

        first_before = rounds[0]["before"]["pages"] if rounds else []
        last_after_rounds = [r for r in rounds if r.get("after")]

        started_failing = len(first_before)
        ended_failing = (
            last_after_rounds[-1]["after"]["still_failed_count"]
            if last_after_rounds else started_failing
        )

        # 각 라운드에서 가장 효과적인 변경
        best_round = None
        best_improvement = 0
        for r in rounds:
            if r.get("delta"):
                improvement = r["delta"].get("net_fixed", 0)
                if improvement > best_improvement:
                    best_improvement = improvement
                    best_round = r["round"]

        session["summary"] = {
            "note": note,
            "total_rounds": len(rounds),
            "started_failing": started_failing,
            "ended_failing": ended_failing,
            "net_fixed": started_failing - ended_failing,
            "best_round": best_round,
            "best_improvement": best_improvement,
            "persistent_failures": _get_persistent_failures(rounds),
        }

    def _find_session(self, session_id: str) -> dict | None:
        for s in self._data["sessions"]:
            if s["id"] == session_id:
                return s
        return None

    def get_session_summary(self, session_id: str) -> dict | None:
        s = self._find_session(session_id)
        return s.get("summary") if s else None


# ──────────────────────────────────────────────
# 내부 헬퍼
# ──────────────────────────────────────────────

def _snapshot_pages(failed_pages: list[dict], prompts_module) -> dict:
    """Before 스냅샷."""
    return {
        "failed_count": len(failed_pages),
        "pages": [
            {
                "path": fp["path"],
                "score": fp.get("score"),
                "issues": fp.get("issues", []),
            }
            for fp in failed_pages
        ],
        "prompt_hashes": {
            "GENERATOR_SYSTEM": _hash_text(prompts_module.GENERATOR_SYSTEM),
            "GENERATOR_USER": _hash_text(prompts_module.GENERATOR_USER),
        },
    }


def _build_after_records(
    before_pages: list[dict],
    newly_failed_paths: set,
    wiki_dir: str,
    extract_spec_fn,
    call_llm,
    backend: str,
) -> list[dict]:
    """재생성 후 각 페이지의 before→after 스코어 기록."""
    before_map = {fp["path"]: fp for fp in before_pages}
    records = []

    for path, before_info in before_map.items():
        passed = path not in newly_failed_paths
        score_after = None
        issues_after = []

        file_path = Path(wiki_dir) / path
        if file_path.exists():
            content = file_path.read_text(encoding="utf-8")
            # plan page 정보 없이도 quick check으로 after score 계산
            result = _quick_check(content)
            score_after = result["score"]
            issues_after = result["issues"]

        records.append({
            "path": path,
            "score_before": before_info.get("score"),
            "score_after": score_after,
            "delta": (score_after - before_info.get("score", 0))
                     if score_after is not None and before_info.get("score") is not None
                     else None,
            "issues_before": before_info.get("issues", []),
            "issues_after": issues_after,
            "passed": passed,
        })

    return records


def _compute_delta(before: dict, after: dict) -> dict:
    """라운드 델타 요약."""
    before_paths = {p["path"] for p in before.get("pages", [])}
    newly_passed = [
        r["path"] for r in after.get("results", [])
        if r["passed"] and r["path"] in before_paths
    ]
    still_failed = [
        r["path"] for r in after.get("results", [])
        if not r["passed"]
    ]
    score_changes = {
        r["path"]: r["delta"]
        for r in after.get("results", [])
        if r.get("delta") is not None
    }
    return {
        "net_fixed": len(newly_passed),
        "newly_passed": newly_passed,
        "still_failed": still_failed,
        "score_changes": score_changes,
        "avg_score_delta": (
            sum(score_changes.values()) / len(score_changes)
            if score_changes else 0
        ),
    }


def _get_persistent_failures(rounds: list[dict]) -> list[str]:
    """모든 라운드에서 여전히 실패한 페이지."""
    if not rounds:
        return []
    last_after = next(
        (r["after"] for r in reversed(rounds) if r.get("after")), None
    )
    if not last_after:
        return []
    return last_after.get("still_failed", [])  # type: ignore[return-value]


def _collect_failed_pages(
    pages: list[dict],
    wiki_dir: str,
    extract_spec_fn,
    call_llm,
    backend: str,
) -> list[dict]:
    """생성된 페이지 중 품질 불합격 목록 수집."""
    failed = []
    for page in pages:
        if not page.get("generated", False):
            continue
        file_path = Path(wiki_dir) / page["path"]
        if not file_path.exists():
            continue
        content = file_path.read_text(encoding="utf-8")
        spec_content = extract_spec_fn(page)
        result = check_quality(content, spec_content, call_llm, backend=backend)
        if not result.get("pass", False):
            failed.append({
                "path": page["path"],
                "score": result.get("score"),
                "issues": result.get("issues", []),
            })
    return failed


def _format_failed_summary(failed_pages: list[dict]) -> str:
    lines = []
    for fp in failed_pages:
        issues_str = "; ".join(fp.get("issues", []))
        lines.append(f"- {fp['path']} (점수: {fp.get('score')}) — {issues_str}")
    return "\n".join(lines)


def _parse_evaluator_response(raw: str) -> dict | None:
    text = re.sub(r'```json\s*', '', raw)
    text = re.sub(r'```\s*', '', text)
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def _hash_text(text: str) -> str:
    """프롬프트 변경 감지용 SHA256 앞 8자."""
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _print_analysis(round_num: int, analysis: dict, failed_pages: list[dict]) -> None:
    print(f"\n{'='*60}")
    print(f"[Evaluate 라운드 {round_num}] 분석 결과:")
    print(f"  불합격: {len(failed_pages)}개 페이지")
    print(f"  원인:   {analysis.get('root_cause', 'N/A')}")
    print(f"  개선 대상 프롬프트: {analysis.get('prompt_fix', {}).get('target', 'N/A')}")
    print(f"  개선 내용: {analysis.get('prompt_fix', {}).get('change', 'N/A')}")
    print(f"  신뢰도: {analysis.get('confidence', 'N/A')}")
    print()
    for fp in failed_pages:
        print(f"  [{fp.get('score', '?')}/8] {fp['path']}")
        for issue in fp.get("issues", []):
            print(f"         → {issue}")
    print(f"{'='*60}")
    print(f"불합격 {len(failed_pages)}개 페이지를 재생성하시겠습니까?")


def _print_round_result(after_records: list[dict]) -> None:
    print(f"\n[재생성 결과]")
    for r in after_records:
        delta_str = f"(+{r['delta']})" if r.get("delta") and r["delta"] > 0 \
                    else (f"({r['delta']})" if r.get("delta") else "")
        status = "합격" if r["passed"] else "불합격"
        print(f"  {status} [{r.get('score_before','?')}→{r.get('score_after','?')}] "
              f"{delta_str} {r['path']}")


def _print_session_summary(history: EvalHistory, session_id: str) -> None:
    summary = history.get_session_summary(session_id)
    if not summary or summary.get("note") == "불합격 없음":
        return
    print(f"\n{'='*60}")
    print(f"[Evaluate 세션 요약]")
    print(f"  총 라운드:    {summary.get('total_rounds', 0)}")
    print(f"  시작 불합격:  {summary.get('started_failing', 0)}개")
    print(f"  최종 불합격:  {summary.get('ended_failing', 0)}개")
    print(f"  개선된 페이지: {summary.get('net_fixed', 0)}개")
    if summary.get("best_round"):
        print(f"  가장 효과적인 라운드: Round {summary['best_round']} "
              f"(+{summary['best_improvement']}개 합격)")
    if summary.get("persistent_failures"):
        print(f"  지속 실패 (수동 검토 필요):")
        for p in summary["persistent_failures"]:
            print(f"    - {p}")
    print(f"{'='*60}")


def _write_eval_log(
    eval_log: str,
    round_num: int,
    failed_pages: list[dict],
    analysis: dict,
) -> None:
    entry = {
        "timestamp": datetime.now().isoformat(),
        "round": round_num,
        "failed_count": len(failed_pages),
        "failed_pages": [fp["path"] for fp in failed_pages],
        "analysis": analysis,
    }
    with open(eval_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
