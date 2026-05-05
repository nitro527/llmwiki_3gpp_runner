"""
query.py — Phase 5: Query

wiki index를 검색하여 관련 페이지를 선택하고 답변을 합성.
2-step stateless 호출:
  1. index.md + 질문 → 관련 페이지 경로 선택 (LLM)
  2. 선택된 페이지 내용 + 질문 → 답변 합성 (LLM)
"""

import json
import logging
import re
from datetime import date
from pathlib import Path

from wiki_builder.prompt_loader import load_prompt

QUERY_SELECTOR_SYSTEM, QUERY_SELECTOR_USER = load_prompt("query_selector")
QUERY_SYNTHESIZER_SYSTEM, QUERY_SYNTHESIZER_USER = load_prompt("query_synthesizer")

logger = logging.getLogger(__name__)

MAX_PAGES = 5
MAX_PAGE_CHARS = 8000   # 페이지 1개 최대 (컨텍스트 절약)


def run_query(question: str, wiki_dir: str, call_llm, file: bool = False) -> dict:
    """
    wiki에서 질문에 답변.

    Returns:
        {
            "answer": str,
            "sources": [str],   # 참조한 페이지 경로
            "filed": str | None  # 저장된 경로 또는 None
        }
    """
    wiki_path = Path(wiki_dir)
    index_path = wiki_path / "index.md"

    if not index_path.exists():
        logger.warning("wiki/index.md 없음 — 빈 index로 진행")
        index_content = "(index 없음)"
    else:
        index_content = index_path.read_text(encoding="utf-8")

    # Step 1: 관련 페이지 선택
    selected_pages = _select_pages(question, index_content, call_llm, wiki_path)
    logger.info(f"선택된 페이지: {selected_pages}")

    # Step 2: 답변 합성
    pages_content = _load_pages(selected_pages, wiki_path)
    answer = _synthesize(question, pages_content, call_llm)

    result = {
        "answer": answer,
        "sources": selected_pages,
        "filed": None,
    }

    # 선택적 filing
    if file:
        filed_path = _file_answer(question, answer, selected_pages, wiki_path)
        result["filed"] = filed_path
        logger.info(f"답변 저장: {filed_path}")

    # log.md 기록
    _append_log(wiki_path, f"query | {question[:80]}")

    return result


def _select_pages(question: str, index_content: str, call_llm, wiki_path: Path) -> list[str]:
    """index.md에서 관련 페이지 경로 선택."""
    user_msg = QUERY_SELECTOR_USER.format(
        question=question,
        index_content=index_content[:20000],  # index 크기 제한
    )
    raw = call_llm(QUERY_SELECTOR_SYSTEM, user_msg, temperature=0.1, json_format=True)

    pages = _parse_json_field(raw, "pages", default=[])
    if not pages:
        logger.warning("페이지 선택 실패, index에서 직접 추출 시도")
        pages = _fallback_page_list(wiki_path)

    # 존재하는 파일만, 최대 MAX_PAGES개
    valid = []
    for p in pages[:MAX_PAGES]:
        full = wiki_path / p
        if full.exists():
            valid.append(p)
        else:
            logger.debug(f"존재하지 않는 페이지 제외: {p}")
    return valid


def _load_pages(page_paths: list[str], wiki_path: Path) -> str:
    """선택된 페이지들의 내용을 하나의 문자열로 조합."""
    parts = []
    for p in page_paths:
        content = (wiki_path / p).read_text(encoding="utf-8")
        if len(content) > MAX_PAGE_CHARS:
            content = content[:MAX_PAGE_CHARS] + "\n...(truncated)"
        parts.append(f"## [{p}]\n{content}")
    return "\n\n".join(parts) if parts else "(참조 페이지 없음)"


def _synthesize(question: str, pages_content: str, call_llm) -> str:
    """관련 페이지 내용으로 답변 합성."""
    user_msg = QUERY_SYNTHESIZER_USER.format(
        question=question,
        pages_content=pages_content,
    )
    answer = call_llm(QUERY_SYNTHESIZER_SYSTEM, user_msg, temperature=0.2)
    if answer.startswith("[LLM 호출 실패]"):
        return f"답변 생성 실패: {answer}"
    return answer


def _file_answer(question: str, answer: str, sources: list[str], wiki_path: Path) -> str:
    """답변을 wiki/query/ 폴더에 저장."""
    query_dir = wiki_path / "query"
    query_dir.mkdir(exist_ok=True)

    today = date.today().strftime("%Y-%m-%d")
    slug = re.sub(r'[^\w가-힣]', '_', question[:40]).strip('_')
    filename = f"{today}_{slug}.md"
    filepath = query_dir / filename

    content = f"# {question}\n\n{answer}\n\n## 참조 페이지\n"
    for s in sources:
        content += f"- [[{Path(s).stem}]]\n"
    content += f"\n## 생성일\n{today}\n"

    filepath.write_text(content, encoding="utf-8")
    return str(filepath.relative_to(wiki_path))


def _append_log(wiki_path: Path, entry: str) -> None:
    """wiki/log.md에 한 줄 추가."""
    log_path = wiki_path / "log.md"
    today = date.today().strftime("%Y-%m-%d")
    line = f"## [{today}] {entry}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


def _fallback_page_list(wiki_path: Path) -> list[str]:
    """index 파싱 실패 시 wiki 디렉토리에서 직접 페이지 목록 수집."""
    pages = []
    for subdir in ["entities", "concepts", "internal"]:
        for md in (wiki_path / subdir).glob("*.md"):
            pages.append(f"{subdir}/{md.name}")
    return pages[:MAX_PAGES]


def _parse_json_field(text: str, field: str, default=None):
    """LLM 응답에서 JSON 블록 파싱 후 특정 필드 추출."""
    # ```json ... ``` 블록 추출
    m = re.search(r'```json\s*([\s\S]+?)\s*```', text)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text.strip())
        return data.get(field, default)
    except Exception:
        return default
