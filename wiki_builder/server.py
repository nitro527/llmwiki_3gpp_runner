"""
server.py — JSON stdio 서버 (sdmAnalyzer 연동용)

stdin에서 JSON 한 줄씩 읽고, stdout에 JSON 한 줄씩 응답.
stdout은 JSON 전용 — 로그/print는 모두 stderr 또는 파일로.

프로토콜:
  요청: {"id": "req-001", "action": "query", "question": "...", "file": false}
  응답: {"id": "req-001", "status": "ok", "answer": "...", "sources": [...]}

지원 action:
  ping    — 생존 확인
  status  — wiki 상태
  query   — wiki 질의
  lint    — wiki 건강 검진
"""

import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def run_server(wiki_dir: str, call_llm) -> None:
    """stdin 루프. EOF 수신 시 정상 종료."""
    from wiki_builder.query import run_query
    from wiki_builder.lint import run_lint

    wiki_path = Path(wiki_dir)
    logger.info("Wiki server 시작 (JSON stdio 모드)")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id", "unknown")
            action = req.get("action", "")

            if action == "ping":
                _respond(req_id, {"status": "pong"})

            elif action == "status":
                _respond(req_id, _get_status(wiki_path))

            elif action == "query":
                question = req.get("question", "").strip()
                if not question:
                    _respond(req_id, {"status": "error", "message": "question 필드 없음"})
                    continue
                file_flag = bool(req.get("file", False))
                result = run_query(question, wiki_dir, call_llm, file=file_flag)
                _respond(req_id, {
                    "status": "ok",
                    "answer": result["answer"],
                    "sources": result["sources"],
                    "filed": result.get("filed"),
                })

            elif action == "lint":
                report = run_lint(wiki_dir, call_llm)
                _respond(req_id, {
                    "status": "ok",
                    "report_path": report.get("report_path"),
                    "orphan_pages": len(report.get("orphan_pages", [])),
                    "broken_links": len(report.get("broken_links", [])),
                    "contradictions": len(report.get("contradictions", [])),
                    "data_gaps": len(report.get("data_gaps", [])),
                    "issues": report,
                })

            else:
                _respond(req_id, {"status": "error", "message": f"알 수 없는 action: {action}"})

        except json.JSONDecodeError as e:
            _respond(req_id or "unknown", {"status": "error", "message": f"JSON 파싱 오류: {e}"})
        except Exception as e:
            logger.exception(f"요청 처리 오류 (id={req_id}): {e}")
            _respond(req_id or "unknown", {"status": "error", "message": str(e)})

    logger.info("Wiki server 종료 (stdin EOF)")


def _respond(req_id: str, payload: dict) -> None:
    """stdout에 JSON 한 줄 출력. flush 필수."""
    payload["id"] = req_id
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _get_status(wiki_path: Path) -> dict:
    """wiki 상태 정보."""
    from datetime import date

    pages = []
    for subdir in ["entities", "concepts", "internal"]:
        d = wiki_path / subdir
        if d.exists():
            pages.extend(d.glob("*.md"))

    index_path = wiki_path / "index.md"
    log_path = wiki_path / "log.md"

    last_build = None
    if log_path.exists():
        import re
        content = log_path.read_text(encoding="utf-8")
        dates = re.findall(r'\[(\d{4}-\d{2}-\d{2})\]', content)
        if dates:
            last_build = dates[-1]

    return {
        "status": "ok",
        "wiki_pages": len(pages),
        "has_index": index_path.exists(),
        "last_build": last_build,
        "wiki_dir": str(wiki_path),
    }
