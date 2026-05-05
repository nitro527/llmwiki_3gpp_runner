"""
orchestrate.py — 파이프라인 조율 (수정 불가)

CLI:
  python wiki_builder/orchestrate.py --phase plan --backend gemini --gemini-key AIza...
  python wiki_builder/orchestrate.py --phase plan --backend claude
  python wiki_builder/orchestrate.py --phase all --backend gptoss \\
      --api-key X --knox-id X --ad-id X

컨텍스트 제한: gpt-oss 기준 128K tokens 설계.
  api.py의 MAX_CONTENT_CHARS(80K chars) / MAX_CONTEXT_CHARS(300K chars) 준수.
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# .env 자동 로드 (없으면 무시)
_env_path = ROOT / ".env"
if _env_path.exists():
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                _k, _v = _k.strip(), _v.strip()
                if _k and _v and _k not in os.environ:
                    os.environ[_k] = _v

import io

_stdout_handler = logging.StreamHandler(
    io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "buffer") else sys.stdout
)
_stdout_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

_file_handler = logging.FileHandler(ROOT / "build.log", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    handlers=[_stdout_handler, _file_handler],
)

import wiki_builder.api as _wiki_api
from wiki_builder.api import truncate_content, call_with_tools
logger = logging.getLogger("orchestrate")


# ──────────────────────────────────────────────
# Orchestrator tool definitions
# ──────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "run_plan",
        "description": (
            "Phase 1: 소스 파일을 청크로 분석하여 wiki 페이지 계획(plan.json)을 생성합니다. "
            "소스 파일 단위로 증분 저장하며, plan.json이 이미 존재하면 새 소스만 추가합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "기존 plan.json을 무시하고 재생성할지 여부 (기본: false)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "run_post_plan",
        "description": (
            "Phase 1.5: plan.json 품질 검증. "
            "중복 섹션 배정 감지 및 페이지 description과 소스 섹션의 의미적 불일치를 수정합니다. "
            "run_plan 완료 후, run_generate 전에 반드시 실행하세요."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_generate",
        "description": (
            "Phase 2: plan.json 기반으로 wiki 마크다운 페이지를 생성합니다. "
            "generated=True인 페이지는 스킵합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_workers": {
                    "type": "integer",
                    "description": "병렬 생성 워커 수 (기본: 1, 무료 티어는 1 권장)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "run_link",
        "description": (
            "Phase 3: 생성된 wiki 페이지 간 역방향 링크([[wikilink]])를 삽입합니다. "
            "linked=True인 페이지는 스킵합니다."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_evaluate",
        "description": (
            "Phase 4: 품질 불합격 페이지를 분석하고 프롬프트를 개선하여 재생성합니다. "
            "최대 5라운드. 터미널에서 사람 확인(y/N) 대화가 발생합니다."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_query",
        "description": (
            "사용자의 자연어 질문을 wiki에서 검색하여 답변합니다. "
            "기술 질문, 개념 설명, 절차 조회 등 모든 wiki 질의에 사용하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "사용자 질문 (자연어)",
                },
                "save": {
                    "type": "boolean",
                    "description": "답변을 wiki/query/에 저장할지 여부 (기본: false)",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "run_plan_features",
        "description": (
            "Phase F1: TS 38.822 feature_priority.json을 읽어 features/ 페이지 계획을 plan.json에 추가합니다. "
            "category 기준으로 그룹핑하며, 지정 release만 처리합니다. "
            "run_generate 완료 후 실행하세요 (관련 절차 링크 정확도를 위해)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "releases": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "처리할 release 목록 (예: [15, 16]). 기본: [15, 16]",
                }
            },
            "required": [],
        },
    },
    {
        "name": "run_generate_features",
        "description": (
            "Phase F2: plan.json의 features/ 페이지를 생성합니다. "
            "기본적으로 Layer-1 (PHY) feature만 생성합니다. "
            "run_plan_features 완료 후 실행하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_workers": {
                    "type": "integer",
                    "description": "병렬 생성 워커 수 (기본: 1)",
                },
                "phy_only": {
                    "type": "boolean",
                    "description": "Layer-1 PHY feature만 생성 (기본: true). false 시 L2/L3/RF 포함.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "run_lint",
        "description": (
            "wiki 전체 건강 검진. 고아 페이지, 깨진 링크, 내용 모순, 데이터 공백을 탐지합니다."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_chat",
        "description": (
            "대화형 REPL 모드를 시작합니다. 블로킹 루프 — 사용자가 /exit를 입력할 때까지 종료되지 않습니다. "
            "사용자가 명시적으로 chat을 요청할 때만 호출하세요."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_server",
        "description": (
            "JSON stdio 서버 모드를 시작합니다. sdmAnalyzer 연동용. 블로킹 루프 — stdin EOF까지 종료되지 않습니다. "
            "사용자가 명시적으로 server를 요청할 때만 호출하세요."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _build_orchestrator_system() -> str:
    agents_md = Path(__file__).parent.parent / "AGENTS.md"
    return agents_md.read_text(encoding="utf-8")


def _build_user_message(args) -> str:
    phase = args.phase
    if phase == "all":
        w = f" (max_workers={args.workers})" if args.workers != 1 else ""
        return f"전체 wiki 빌드 파이프라인을 실행하세요: plan → generate{w} → link → evaluate → lint (lint 결과에 따라 피드백 루프 판단)"
    elif phase == "plan":
        return "Phase 1(run_plan)을 실행하세요."
    elif phase == "post_plan":
        return "Phase 1.5(run_post_plan)을 실행하세요."
    elif phase == "generate":
        w = f" max_workers={args.workers}" if args.workers != 1 else ""
        return f"Phase 2(run_generate)를 실행하세요.{w}"
    elif phase == "link":
        return "Phase 3(run_link)를 실행하세요."
    elif phase == "evaluate":
        return "Phase 4(run_evaluate)를 실행하세요."
    elif phase == "lint":
        return "run_lint를 실행하세요."
    else:
        return f"phase={phase}를 실행하세요."


# ──────────────────────────────────────────────
# 경로 설정
# ──────────────────────────────────────────────

SOURCES_DIR          = ROOT / "sources"
WIKI_DIR             = ROOT / "wiki"
PLAN_PATH            = ROOT / "plan.json"
EVAL_LOG             = ROOT / "eval.log"
FEATURE_PRIORITY_PATH = ROOT / "feature_priority.json"
REF_38822_PATH       = ROOT / "sources" / "3gpp_ref" / "38822-i00.docx"


def _load_feature_list() -> list | None:
    """feature_priority.json 로드. 없으면 38.822 docx에서 자동 생성."""
    if FEATURE_PRIORITY_PATH.exists():
        import json as _json
        with open(FEATURE_PRIORITY_PATH, encoding="utf-8") as f:
            feats = _json.load(f)
        logger.info(f"feature_priority.json 로드: {len(feats)}개")
        return feats

    if REF_38822_PATH.exists():
        logger.info("feature_priority.json 없음 → 38.822 자동 파싱 중...")
        from wiki_builder.parse_38822 import parse_feature_list
        import json as _json
        try:
            feats = parse_feature_list(str(REF_38822_PATH))
            with open(FEATURE_PRIORITY_PATH, "w", encoding="utf-8") as f:
                _json.dump(feats, f, ensure_ascii=False, indent=2)
            logger.info(f"feature_priority.json 생성 완료: {len(feats)}개")
            return feats
        except Exception as e:
            logger.warning(f"38.822 파싱 실패 (feature hint 비활성): {e}")
            return None

    logger.info("38.822 docx 없음 — feature hint 비활성")
    return None


# ──────────────────────────────────────────────
# 섹션 추출 유틸
# ──────────────────────────────────────────────

def _extract_section(text: str, section_num: str) -> str:
    """
    텍스트에서 섹션 번호에 해당하는 내용 추출.

    패턴: \\n{섹션번호}\\t{제목} 로 시작,
    다음 같은 레벨 이상의 헤더까지.

    예: section_num="6.3.1" → \\n6.3.1\\t...부터 \\n6.3.2\\t 또는 \\n6.4\\t 전까지
    """
    escaped = re.escape(section_num)
    pattern = re.compile(rf'\n{escaped}\t')
    m = pattern.search(text)
    if not m:
        return ""

    start = m.start()

    # 같은 레벨 이상 다음 섹션 찾기
    # 섹션 레벨: "6.3.1" → depth=3, 다음에 나올 수 있는 헤더는 6.3.x 이상 레벨
    parts = section_num.split(".")
    depth = len(parts)

    # 현재 섹션 번호의 상위 prefix (depth-1 레벨까지)
    # depth=1 → 다음 숫자절, depth=2 → 같은 상위에서 다음 절, etc.
    if depth == 1:
        # 다음 최상위 섹션 (\n\d+\t)
        next_pat = re.compile(r'\n\d+\t')
    else:
        parent_prefix = re.escape(".".join(parts[:-1]))
        # 같은 부모 아래 다음 섹션 또는 상위 섹션
        next_pat = re.compile(
            rf'\n(?:{parent_prefix}\.\d+\t|\d+(?:\.\d+){{0,{depth-2}}}\t)'
        )

    m2 = next_pat.search(text, start + 1)
    end = m2.start() if m2 else len(text)

    return text[start:end].strip()


def extract_spec_content(plan_page: dict) -> str:
    """
    plan 페이지의 sources 기반으로 스펙 내용 추출.
    소스 파일을 읽고 지정된 섹션 내용만 조합.
    """
    from chunk_text import read_file_content

    parts = []
    for src in plan_page.get("sources", []):
        src_file = src.get("file", "")
        sections = src.get("sections", [])

        # 절대 경로 구성
        candidates = [
            ROOT / src_file,
            SOURCES_DIR / src_file,
            SOURCES_DIR / "3gpp" / Path(src_file).name,
        ]
        full_path = None
        for c in candidates:
            if c.exists():
                full_path = c
                break

        if full_path is None:
            logger.warning(f"소스 파일 없음: {src_file}")
            continue

        try:
            content = read_file_content(str(full_path))
        except Exception as e:
            logger.error(f"파일 읽기 실패 ({src_file}): {e}")
            continue

        if sections:
            for sec in sections:
                extracted = _extract_section(content, sec)
                if extracted:
                    parts.append(f"[{src_file} §{sec}]\n{extracted}")
        else:
            # 섹션 미지정 시 전체 (MAX_CONTENT_CHARS 상한)
            parts.append(f"[{src_file}]\n{content[:_wiki_api.MAX_CONTENT_CHARS]}")

    combined = "\n\n".join(parts) if parts else "(스펙 내용 없음)"
    # 전체 합산도 MAX_CONTENT_CHARS 이하로 보장
    return truncate_content(combined, _wiki_api.MAX_CONTENT_CHARS, label="spec_content")


# ──────────────────────────────────────────────
# 메인 파이프라인
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LLMWiki 파이프라인")
    parser.add_argument("--phase",
                        choices=["plan", "post_plan", "generate", "link", "evaluate", "all",
                                 "query", "lint", "chat", "server"],
                        default="all")
    parser.add_argument("--backend", choices=["claude", "gemini", "gptoss", "ollama"], default="claude")
    # gpt-oss 인증
    parser.add_argument("--api-key", default="", help="gpt-oss API key")
    parser.add_argument("--knox-id", default="")
    parser.add_argument("--ad-id", default="")
    # Gemini 옵션
    parser.add_argument("--gemini-key", default="", help="Gemini API key (또는 GEMINI_API_KEY 환경변수)")
    parser.add_argument("--gemini-model", default="", help="Gemini 모델 (기본: gemini-2.5-flash-lite)")
    # Ollama 옵션
    parser.add_argument("--ollama-url", default="", help="Ollama 서버 URL (기본: http://localhost:11434)")
    parser.add_argument("--ollama-model", default="", help="Ollama 모델 (기본: gemma4)")
    parser.add_argument("--ollama-context", type=int, default=0, help="Ollama context window (기본: 16384)")
    parser.add_argument("--workers", type=int, default=1,
                        help="Generate 병렬 수 (기본: 1, 무료 티어는 1 권장)")
    parser.add_argument("--question", default="", help="Query phase용 질문")
    parser.add_argument("--file", action="store_true", help="Query 답변을 wiki에 저장")
    args = parser.parse_args()

    # 백엔드 설정
    os.environ["WIKI_BACKEND"] = args.backend

    from wiki_builder.api import call_simple, configure_gptoss, configure_gemini, configure_ollama
    if args.backend == "gptoss":
        configure_gptoss(
            api_key=args.api_key,
            knox_id=args.knox_id,
            ad_id=args.ad_id,
        )
    elif args.backend == "gemini":
        configure_gemini(
            api_key=args.gemini_key,
            model=args.gemini_model,
        )
    elif args.backend == "ollama":
        configure_ollama(
            base_url=args.ollama_url,
            model=args.ollama_model,
            context_window=args.ollama_context,
        )

    # call_llm wrapper: backend 인자 자동 주입
    def call_llm(system, user, temperature=0.3, **kwargs):
        kwargs.pop("backend", None)
        return call_simple(system, user, temperature=temperature,
                           backend=args.backend, **kwargs)

    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    (WIKI_DIR / "entities").mkdir(exist_ok=True)
    (WIKI_DIR / "concepts").mkdir(exist_ok=True)
    (WIKI_DIR / "internal").mkdir(exist_ok=True)

    # ── Query: tool 방식 아님 — 2-step stateless 그대로 ──
    if args.phase == "query":
        if not args.question:
            logger.error("--question 인자 필요: --phase query --question '질문'")
            sys.exit(1)
        logger.info("=== Phase 5: Query ===")
        (WIKI_DIR / "query").mkdir(exist_ok=True)
        from wiki_builder.query import run_query
        result = run_query(
            question=args.question,
            wiki_dir=str(WIKI_DIR),
            call_llm=call_llm,
            file=args.file,
        )
        print("\n" + result["answer"])
        if result["sources"]:
            print(f"\n[참조: {', '.join(result['sources'])}]")
        if result.get("filed"):
            print(f"[저장됨: {result['filed']}]")
        return

    # ── Chat: REPL — 입력을 Orchestrator에 직접 전달 ──
    if args.phase == "chat":
        logger.info("=== Chat REPL 시작 ===")
        from wiki_builder.chat import run_chat
        def _orchestrate(message: str):
            _run_orchestrator(args, call_llm, user_message=message)
        run_chat(_orchestrate)
        return

    if args.phase == "server":
        logging.getLogger().removeHandler(_stdout_handler)
        logger.info("=== Server 모드 시작 ===")
        from wiki_builder.server import run_server
        run_server(wiki_dir=str(WIKI_DIR), call_llm=call_llm)
        return

    # ── Orchestrator LLM 에이전트 루프 ──────────────────────
    _run_orchestrator(args, call_llm)
    logger.info("=== 파이프라인 종료 ===")


def _run_orchestrator(args, call_llm, user_message: str = None):
    """
    Orchestrator LLM 에이전트가 tool 호출을 통해 각 Phase를 실행.
    user_message가 주어지면 argparse phase 무시하고 해당 메시지를 직접 사용 (chat REPL용).
    """
    from chunk_text import chunk_file
    from wiki_builder.evaluate import check_quality

    # 모든 tool이 공유하는 context (Python 클로저로 전달)
    ctx: dict = {
        "call_llm": call_llm,
        "backend": args.backend,
        "max_workers": args.workers,
        "generate_failed": [],  # run_generate → run_evaluate 전달용
        "feature_list": _load_feature_list(),
    }

    def execute_tool(name: str, tool_input: dict) -> str:
        """tool 이름과 input을 받아 실행하고 결과 문자열 반환."""
        try:
            if name == "run_plan":
                from wiki_builder.plan import run_plan
                if tool_input.get("force") and PLAN_PATH.exists():
                    PLAN_PATH.unlink()
                plan = run_plan(
                    sources_dir=str(SOURCES_DIR),
                    wiki_dir=str(WIKI_DIR),
                    plan_path=str(PLAN_PATH),
                    call_llm=ctx["call_llm"],
                    chunk_fn=chunk_file,
                    backend=ctx["backend"],
                )
                return f"Plan 완료: {len(plan.get('pages', []))}개 페이지"

            elif name == "run_post_plan":
                from wiki_builder.post_plan import run_post_plan
                plan = _load_plan()
                if not plan:
                    return "[오류] plan.json 없음. run_plan을 먼저 실행하세요."
                plan = run_post_plan(
                    plan=plan,
                    plan_path=str(PLAN_PATH),
                    call_llm=ctx["call_llm"],
                    backend=ctx["backend"],
                )
                pages = plan.get("pages", [])
                return (
                    f"Post-Plan 완료: {len(pages)}개 페이지 검증 완료"
                )

            elif name == "run_generate":
                from wiki_builder.generate import run_generate
                plan = _load_plan()
                if not plan:
                    return "[오류] plan.json 없음. run_plan을 먼저 실행하세요."
                failed = run_generate(
                    plan=plan,
                    wiki_dir=str(WIKI_DIR),
                    plan_path=str(PLAN_PATH),
                    call_llm=ctx["call_llm"],
                    extract_spec_fn=extract_spec_content,
                    check_quality_fn=check_quality,
                    backend=ctx["backend"],
                    max_workers=tool_input.get("max_workers", ctx["max_workers"]),
                    feature_list=ctx["feature_list"],
                )
                ctx["generate_failed"] = failed
                return f"Generate 완료. 불합격: {len(failed)}개"

            elif name == "run_link":
                from wiki_builder.link import run_link
                plan = _load_plan()
                if not plan:
                    return "[오류] plan.json 없음."
                run_link(
                    plan=plan,
                    wiki_dir=str(WIKI_DIR),
                    plan_path=str(PLAN_PATH),
                    call_llm=ctx["call_llm"],
                    backend=ctx["backend"],
                )
                return "Link 완료"

            elif name == "run_evaluate":
                from wiki_builder.evaluate import run_evaluate
                plan = _load_plan()
                if not plan:
                    return "[오류] plan.json 없음."
                run_evaluate(
                    plan=plan,
                    wiki_dir=str(WIKI_DIR),
                    plan_path=str(PLAN_PATH),
                    eval_log=str(EVAL_LOG),
                    call_llm=ctx["call_llm"],
                    extract_spec_fn=extract_spec_content,
                    backend=ctx["backend"],
                    initial_failed=ctx["generate_failed"],
                )
                return "Evaluate 완료"

            elif name == "run_query":
                from wiki_builder.query import run_query
                question = tool_input.get("question", "")
                save = tool_input.get("save", False)
                result = run_query(question, str(WIKI_DIR), ctx["call_llm"], file=save)
                out = result["answer"]
                if result.get("sources"):
                    out += f"\n\n[참조: {', '.join(result['sources'])}]"
                if result.get("filed"):
                    out += f"\n[저장됨: {result['filed']}]"
                return out

            elif name == "run_plan_features":
                from wiki_builder.parse_38822 import build_feature_groups
                from wiki_builder.prompt_loader import load_prompt
                _FEATURE_GENERATOR_SYSTEM, FEATURE_GENERATOR_USER = load_prompt("feature_generator")
                import json as _json

                releases = tool_input.get("releases", [15, 16])
                feat_list = ctx.get("feature_list") or []
                if not feat_list:
                    return "[오류] feature_priority.json 없음. 38.822 파일을 sources/3gpp_ref/에 넣고 재시작하세요."

                filtered = [f for f in feat_list if f["release"] in releases]
                groups = build_feature_groups(filtered)

                plan = _load_plan()
                if not plan:
                    plan = {"planned_sources": [], "pages": []}

                existing_paths = {p["path"] for p in plan["pages"]}
                added = 0
                for g in groups:
                    path = f"features/{g['page_name']}.md"
                    if path in existing_paths:
                        continue
                    plan["pages"].append({
                        "path": path,
                        "description": g["category"],
                        "generated": False,
                        "linked": False,
                        "sources": [],          # 38.822 데이터는 generate 시 직접 주입
                        "feature_group": g,     # generate_features가 읽을 메타데이터
                    })
                    existing_paths.add(path)
                    added += 1

                with open(PLAN_PATH, "w", encoding="utf-8") as _f:
                    _json.dump(plan, _f, ensure_ascii=False, indent=2)

                (WIKI_DIR / "features").mkdir(exist_ok=True)
                return f"plan_features 완료: {added}개 features/ 페이지 추가 (Rel{releases})"

            elif name == "run_generate_features":
                import json as _json
                from wiki_builder.parse_38822 import _MANDATORY_LABEL

                plan = _load_plan()
                if not plan:
                    return "[오류] plan.json 없음."

                phy_only = tool_input.get("phy_only", True)

                def _is_phy(page: dict) -> bool:
                    """feature_group 내 feature 중 하나라도 Layer-1이면 PHY로 판단."""
                    feats = page.get("feature_group", {}).get("features", [])
                    return any("Layer-1" in f.get("work_item", "") for f in feats)

                todo = [p for p in plan["pages"]
                        if p["path"].startswith("features/") and not p.get("generated")
                        and (not phy_only or _is_phy(p))]
                if not todo:
                    return "생성할 features/ 페이지 없음 (모두 완료되었거나 plan_features 미실행)"

                wiki_pages = [p["path"] for p in plan["pages"]]
                wiki_page_list = "\n".join(wiki_pages)

                from wiki_builder.prompt_loader import load_prompt
                FEATURE_GENERATOR_SYSTEM, FEATURE_GENERATOR_USER = load_prompt("feature_generator")
                max_workers = tool_input.get("max_workers", ctx["max_workers"])
                succeeded = 0
                failed_list = []

                for page in todo:
                    g = page.get("feature_group", {})
                    if not g:
                        logger.warning(f"feature_group 메타데이터 없음: {page['path']}")
                        continue

                    # Feature 목록 테이블 생성
                    lines = ["| Index | Feature | Field name (TS 38.331) | Rel | Status |",
                             "|-------|---------|----------------------|-----|--------|"]
                    for f in g.get("features", []):
                        label = _MANDATORY_LABEL.get(f["mandatory"], "?")
                        field = f.get("field_name") or "n/a"
                        lines.append(
                            f"| {f['index']} | {f['feature_group'][:60]} "
                            f"| {field} | Rel-{f['release']} | {label} |"
                        )
                    feature_table = "\n".join(lines)

                    # cross-category prereq 요약
                    cross = g.get("cross_category_prereqs", [])
                    if cross:
                        cross_summary = "\n".join(
                            f"- {c['index']}: {c['feature_group']}" for c in cross
                        )
                    else:
                        cross_summary = "(없음)"

                    user_msg = FEATURE_GENERATOR_USER.format(
                        page_name=g["page_name"],
                        feature_table=feature_table,
                        cross_prereq_summary=cross_summary,
                        wiki_page_list=wiki_page_list,
                    )

                    raw = ctx["call_llm"](
                        FEATURE_GENERATOR_SYSTEM,
                        user_msg,
                        temperature=0.3,
                        backend=ctx["backend"],
                    )

                    if raw.startswith("[LLM 호출 실패]"):
                        logger.error(f"Feature 생성 실패: {page['path']}")
                        failed_list.append(page["path"])
                        continue

                    out_path = WIKI_DIR / page["path"]
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(raw, encoding="utf-8")

                    page["generated"] = True
                    succeeded += 1
                    logger.info(f"  생성 완료: {page['path']}")

                with open(PLAN_PATH, "w", encoding="utf-8") as _f:
                    _json.dump(plan, _f, ensure_ascii=False, indent=2)

                return f"generate_features 완료: {succeeded}개 생성, {len(failed_list)}개 실패"

            elif name == "run_lint":
                from wiki_builder.lint import run_lint, run_post_lint
                report = run_lint(wiki_dir=str(WIKI_DIR), call_llm=ctx["call_llm"])

                plan = _load_plan()
                if plan:
                    post = run_post_lint(
                        report=report,
                        plan=plan,
                        plan_path=str(PLAN_PATH),
                    )
                    if post["needs_generate"]:
                        execute_tool("run_generate", {})
                    if post["needs_link"]:
                        execute_tool("run_link", {})
                else:
                    logger.warning("plan.json 없음 — post-lint 후속 조치 스킵")
                    post = {}

                summary = (
                    f"Lint 완료 — "
                    f"고아:{len(report.get('orphan_pages', []))} "
                    f"깨진링크:{len(report.get('broken_links', []))} "
                    f"모순:{len(report.get('contradictions', []))} "
                    f"공백:{len(report.get('data_gaps', []))}"
                )
                added = len(post.get("added_pages", []))
                reset = len(post.get("reset_pages", []))
                relink = len(post.get("relink_pages", []))
                if added or reset or relink:
                    summary += f" | 후속: 추가={added} 재생성={reset} 재링크={relink}"
                return summary

            elif name == "run_chat":
                from wiki_builder.chat import run_chat
                run_chat(wiki_dir=str(WIKI_DIR), call_llm=ctx["call_llm"])
                return "Chat 종료"

            elif name == "run_server":
                logging.getLogger().removeHandler(_stdout_handler)
                from wiki_builder.server import run_server
                run_server(wiki_dir=str(WIKI_DIR), call_llm=ctx["call_llm"])
                return "Server 종료"

            else:
                return f"[오류] 알 수 없는 tool: {name}"

        except Exception as e:
            logger.exception(f"Tool 실행 오류 ({name}): {e}")
            return f"[오류] {name} 실행 실패: {e}"

    # ── Orchestrator 에이전트 루프 ──────────────────────────
    system = _build_orchestrator_system()
    user_content = user_message if user_message is not None else _build_user_message(args)
    messages = []

    # 첫 user 메시지 (백엔드별 형식)
    if args.backend == "gemini":
        messages.append({"role": "user", "parts": [{"text": user_content}]})
    else:
        messages.append({"role": "user", "content": user_content})

    MAX_ITERATIONS = 20
    for iteration in range(MAX_ITERATIONS):
        logger.info(f"Orchestrator 루프 {iteration + 1}/{MAX_ITERATIONS}")

        response = call_with_tools(
            system=system,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            temperature=0.1,
            backend=args.backend,
        )

        if response["stop_reason"] == "error":
            logger.error(f"Orchestrator LLM 호출 실패: {response['text']}")
            break

        # assistant 응답을 messages에 누적 (백엔드별 형식)
        raw = response["raw"]
        if args.backend == "claude":
            messages.append({"role": "assistant", "content": raw})
        else:
            messages.append(raw)  # Gemini: {"role":"model",...} / gptoss: {"role":"assistant",...}

        if response["text"]:
            logger.info(f"Orchestrator: {response['text']}")

        if response["stop_reason"] == "end_turn":
            logger.info("Orchestrator 완료 (end_turn)")
            break

        if response["stop_reason"] == "tool_use":
            tool_results = []
            for tc in response["tool_calls"]:
                logger.info(f"Tool 호출: {tc['name']} {tc['input']}")
                result = execute_tool(tc["name"], tc["input"])
                logger.info(f"Tool 결과: {result[:200]}")
                tool_results.append({"id": tc["id"], "name": tc["name"], "result": result})

            # tool 결과를 messages에 추가 (백엔드별 형식)
            if args.backend == "claude":
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tr["id"],
                            "content": tr["result"],
                        }
                        for tr in tool_results
                    ],
                })
            elif args.backend == "gemini":
                messages.append({
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": tr["name"],
                                "response": {"content": tr["result"]},
                            }
                        }
                        for tr in tool_results
                    ],
                })
            else:  # gptoss / ollama (OpenAI 호환 형식)
                for tr in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr["id"],
                        "content": tr["result"],
                    })
    else:
        logger.warning(f"Orchestrator 최대 반복 {MAX_ITERATIONS}회 초과 — 강제 종료")


def _load_plan() -> dict | None:
    if not PLAN_PATH.exists():
        return None
    with open(PLAN_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    main()
