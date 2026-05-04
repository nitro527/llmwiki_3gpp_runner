"""
chat.py — 대화형 REPL 인터페이스 (터미널용)

사용자 입력을 Orchestrator LLM에 직접 전달.
Orchestrator가 run_query / run_plan / run_generate 등 적절한 tool을 판단해 실행.
"""

HELP_TEXT = """
=== LLMWiki Chat ===
질문이나 명령을 자유롭게 입력하세요.
Orchestrator가 적절한 작업을 판단해 실행합니다.

예시:
  PUSCH scrambling 절차를 설명해줘
  plan 다시 짜줘
  generate 시작해
  wiki 상태 알려줘

종료: /exit
"""


def run_chat(orchestrate_fn) -> None:
    """
    대화형 REPL 루프.

    Args:
        orchestrate_fn: 사용자 입력 문자열을 받아 Orchestrator를 실행하는 callable
    """
    print(HELP_TEXT)

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            break

        if not user_input:
            continue

        if user_input in ("/exit", "exit", "quit", "종료"):
            print("종료합니다.")
            break

        orchestrate_fn(user_input)
        print()
