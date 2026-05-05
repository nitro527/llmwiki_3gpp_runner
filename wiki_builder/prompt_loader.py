"""
prompt_loader.py — sub_agents/*.md 파일에서 프롬프트 로드

load_prompt(agent_name) -> (system, user)
    sub_agents/{agent_name}.md를 읽어 system/user 프롬프트 튜플 반환.
    파일 경로는 이 모듈 기준으로 계산 — 어디서 실행해도 동작.
"""

from pathlib import Path

# wiki_builder/ 패키지 디렉토리에서 두 단계 위가 프로젝트 루트
_PROJECT_ROOT = Path(__file__).parent.parent
_SUB_AGENTS_DIR = _PROJECT_ROOT / "sub_agents"

_SEPARATOR = "\n---USER---\n"


def load_prompt(agent_name: str) -> tuple[str, str]:
    """
    sub_agents/{agent_name}.md를 읽어 (system, user) 튜플 반환.

    파일 형식:
        [system 프롬프트]

        ---USER---

        [user 프롬프트 — {변수명} 플레이스홀더 포함 가능]

    Raises:
        FileNotFoundError: .md 파일이 존재하지 않을 때
        ValueError: ---USER--- 구분자가 없을 때
    """
    md_path = _SUB_AGENTS_DIR / f"{agent_name}.md"

    if not md_path.exists():
        raise FileNotFoundError(
            f"프롬프트 파일 없음: {md_path}\n"
            f"sub_agents/{agent_name}.md 파일을 생성하세요."
        )

    text = md_path.read_text(encoding="utf-8")

    if _SEPARATOR not in text:
        raise ValueError(
            f"{md_path}: '---USER---' 구분자가 없습니다.\n"
            "파일 형식: [system]\n\n---USER---\n\n[user]"
        )

    system_part, user_part = text.split(_SEPARATOR, maxsplit=1)
    return system_part.strip(), user_part.strip()
