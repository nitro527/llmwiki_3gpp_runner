# CLAUDE.md — LLMWiki 개발 지침 (Claude Code 전용)

> **이 파일의 독자**: Claude Code (개발 도구).
> Orchestrator LLM 시스템 프롬프트는 `AGENTS.md`, 서브에이전트 프롬프트는 `sub_agents/*.md`를 볼 것.

---

## 파일 역할 구분

| 파일 | 독자 | 내용 |
|------|------|------|
| `CLAUDE.md` (이 파일) | Claude Code | 구현 방법, 아키텍처, 테스트 |
| `AGENTS.md` | Orchestrator LLM | Orchestrator 시스템 프롬프트 (`orchestrate.py`가 직접 읽음) |
| `sub_agents/*.md` | 각 서브에이전트 LLM | phase별 system/user 프롬프트 (`---USER---` 구분자로 분리) |

**Claude Code 개발 중에는 모든 파일 수정 가능.**
예외: `sources/`(원본 소스), `KARPATHY_LLMWIKI.md`(원문) — 읽기만.

---

## 프로젝트 목적

5G NR PHY 스펙(3GPP) → LLM용 지식베이스(wiki) 자동 구축·유지·조회.
Karpathy LLM Wiki 패턴 구현체. 원문: `KARPATHY_LLMWIKI.md`
요구사항: `PRD.md` / 구현 순서: `TASKS.md` / Orchestrator 시스템 프롬프트: `AGENTS.md` / 서브에이전트 프롬프트: `sub_agents/*.md`

---

## 실행 환경

- **Claude Code**: 이 코드를 *만드는* 도구. 구현에만 사용.
- **실행 시**: Claude Code 불필요. `python wiki_builder/orchestrate.py`로 단독 실행.
- **LLM 독립성**: `api.py`의 `call_simple(system, user)`만 구현하면
  Claude / gpt-oss / GPT-4 / 로컬 LLM 등 어떤 백엔드로도 교체 가능.
  파이프라인 코드(plan.py, generate.py 등)는 LLM 종류를 모름.

---

## api.py 구현 규칙

```python
# 환경변수로 백엔드 전환
BACKEND = os.getenv("WIKI_BACKEND", "gptoss")  # "claude" | "gptoss" | "gemini"

def call_simple(system: str, user: str, **kwargs) -> str:
    # 실패 시 반드시 "[LLM 호출 실패] ..." 형태로 반환
    # 예외를 raise하지 말 것
```

- `max_tokens` 기본값: 16384 (모든 백엔드 공통)

**Claude 백엔드:**
- 모델: `claude-sonnet-4-5` 이상
- 환경변수: `ANTHROPIC_API_KEY`
- temperature: 0.1 (Planner/Checker), 0.3 (Generator)

**gpt-oss 백엔드:**
- URL: `http://apigw-stg.samsungds.net:8000/gpt-oss/1/openai/gpt-oss-120b/v1/chat/completions`
- 모델명: `"openai/gpt-oss-120b"`
- 인증 헤더: `x-dep-ticket` (Bearer 방식 아님), `Send-System-Name: Tracer`, `User-Id`, `User-Type`
- proxies: `{"http": None, "https": None}` 필수
- timeout: 300

**Gemini 백엔드:**
- 기본 모델: `gemini-3.1-flash-lite-preview` (무료 RPD 500)
- 환경변수: `GEMINI_API_KEY`
- 모델 오버라이드: `GEMINI_MODEL` 환경변수

---

## plan.json 스키마

```json
{
  "post_plan_done": false,
  "planned_sources": [
    "sources\\3gpp\\38211-i90.docx",
    "sources\\3gpp\\38212-i80.docx"
  ],
  "pages": [
    {
      "path": "entities/PUSCH.md",
      "description": "한 줄 설명",
      "generated": false,
      "linked": false,
      "sources": [
        {
          "file": "sources\\3gpp\\38211-i90.docx",
          "sections": ["6.3.1", "6.3.1.1"]
        },
        {
          "file": "sources\\3gpp\\38212-i80.docx",
          "sections": ["6.2.1"]
        }
      ]
    }
  ]
}
```

- `planned_sources`: 이미 플래닝된 소스 파일 목록. 재실행 시 이 목록에 없는 파일만 증분 플래닝.
- `post_plan_done`: Post-Plan 검증 완료 여부. `true`면 재실행 시 스킵.
- `sources`: 한 페이지가 여러 소스에 걸쳐 있을 경우 배열로 머지됨 (멀티소스).

---

## 런타임 에이전트 구조 (구현 참고용)

> 아래는 Claude Code가 무엇을 구현해야 하는지 이해하기 위한 요약이다.
> **규칙의 권위는 `AGENTS.md`에 있다. 이 섹션은 구현 지침이지 에이전트 행동 규칙이 아니다.**

### 멀티에이전트 파이프라인

```
orchestrate.py
└── Orchestrator LLM (에이전트)
      ├── tool: run_plan()       Phase 1:   Planner    (소스 파일 단위 증분 저장)
      ├── tool: run_post_plan()  Phase 1.5: Post-Plan  (plan.json 품질 검증)
      ├── tool: run_generate()   Phase 2:   Generator + Quality Checker
      ├── tool: run_link()       Phase 3:   Linker
      ├── tool: run_evaluate()   Phase 4:   Evaluator
      ├── tool: run_query()      Phase 5:   Query (Orchestrator 경유)
      ├── tool: run_lint()       Phase 6:   Lint + Post-Lint 후속 조치
      ├── tool: run_chat()       Chat REPL (tool로 등록되어 있으나 실제로는 REPL 진입점)
      └── tool: run_server()     Server

--phase query (CLI)  ← 예외: 스크립트 호환성 위해 2-step stateless 직접 실행도 유지
```

**핵심:**
- Orchestrator LLM이 상황 판단하여 tool 호출 순서 결정
- Chat REPL은 사용자 입력을 Orchestrator에 직접 전달 — 커맨드 라우팅 없음
- run_query는 Orchestrator tool로도 동작, --phase query는 직접 경로로도 동작
- run_lint 완료 후 run_post_lint 자동 실행 — 이슈별 사용자 확인 후 run_generate/run_link 연쇄 호출

### 구현 시 핵심 제약 (AGENTS.md에서 가져옴)

각 Phase를 구현할 때 반드시 지켜야 하는 제약:

| 제약 | 이유 |
|------|------|
| LLM 호출은 `call_simple(system, user)` 단일 형태만 사용 | stateless, messages 누적 없음 |
| Planner: 소스 파일 완료 시마다 plan.json 저장 | 중간 crash 복구 |
| Planner: existing_pages를 `path: description` 형태로 LLM 전달 | 기존 범위 인지 |
| Post-Plan: 코드 검증(중복 섹션) + LLM 검증(의미적 불일치) | plan 품질 보장 |
| Generator: 입력에 섹션 내용 + `path: description` 목록 전달 | 컨텍스트 과부하 방지 |
| Generator: hallucination 감지 후 저장 (3어절 5회 반복 → 차단) | 품질 보장 |
| Generator: QUALITY_RETRY_MAX 초과 시 `failed=True` 처리 → Evaluator로 넘김 (최고점수 저장 방식 폐기) | 불량 페이지 generated=True 방지 |
| evaluate.py `check_quality()`: content/spec_content 전체 전달 (잘라서 전달 방식 폐기), `feature_hint` 파라미터 추가 | 평가 정확도 향상 |
| plan.py: LLM 재시도 루프를 `run_plan`의 `while True`로 올림, 불완전 JSON 복구 로직 포함 | max_tokens 잘림 대응 |
| plan.json 절대 삭제 금지 — generated/linked/post_plan_done 플래그로 재개 | 체크포인트 |
| LLM 호출 실패: 최대 3회 재시도, exponential backoff (5→10→20초) | 안정성 |
| 429: 65초 대기 후 재시도 (MAX_RETRIES 카운트 제외) | Rate limit 처리 |
| 예외 raise 금지 — 로그 기록 후 계속 진행 | 파이프라인 중단 방지 |
| Server 모드: stdout은 JSON 전용, 로그는 stderr/파일만 | sdmAnalyzer 프로토콜 |
| Query: 페이지 선택 최대 5개 | 컨텍스트 과부하 방지 |
| Lint: LLM에게 전체 wiki 한 번에 전달 금지 — 5페이지 묶음 단위 | 컨텍스트 과부하 방지 |
| Post-Lint: broken_links → plan 추가 / missing_backlinks → linked 리셋 / contradictions → generated 리셋 | Lint 후 자동 복구 |
| Post-Lint 후속: `run_generate` 실패 시 `run_link` 차단 (`_run_post_lint_followup`) | 부분 생성 상태에서 링크 걸지 않기 위해 |

---

## 프롬프트 수정 방법

프롬프트를 수정할 때는 `sub_agents/` 디렉토리의 해당 .md 파일을 직접 편집한다.

| 에이전트 | 파일 |
|---------|------|
| Planner | `sub_agents/planner.md` |
| Post-Plan | `sub_agents/post_plan.md` |
| Generator | `sub_agents/generator.md` |
| Checker | `sub_agents/checker.md` |
| Linker | `sub_agents/linker.md` |
| Evaluator | `sub_agents/evaluator.md` |
| Query Selector | `sub_agents/query_selector.md` |
| Query Synthesizer | `sub_agents/query_synthesizer.md` |
| Lint | `sub_agents/lint.md` |
| Feature Generator | `sub_agents/feature_generator.md` |

각 .md 파일 포맷: system 프롬프트 본문 + `---USER---` 구분자 + user 프롬프트 템플릿.
`wiki_builder/prompt_loader.py`의 `load_prompt(agent_name) -> tuple[str, str]`로 읽는다.

Evaluator가 프롬프트를 개선할 때도 해당 `sub_agents/*.md` 파일을 재작성한다.

**프롬프트 변수 추가 시 주의:** `sub_agents/*.md`의 `{변수명}` placeholder와 해당 `format(변수명=...)` 호출을 반드시 동시에 추가. 불일치 시 KeyError 발생.

---

## 구현 시 참고

- `chunk_text.py`의 `read_file_content()`, `find_chunk_boundary()` 재사용
- 섹션 추출: `\n{섹션번호}\t` 패턴으로 시작점 찾고 다음 같은 레벨 헤더까지
- 약어 추출: `\n3.3\t` 두 번째 매치에서 시작, `\n4\t`까지
- 3GPP spec 청킹: 섹션 경계 우선, MIN_CHUNK=40000자, MAX_CHUNK=50000자
- **backend 파라미터 규칙:** 모든 phase 함수의 `backend` 기본값은 `None`. 본문 첫 줄에서 `import wiki_builder.api; backend = backend or wiki_builder.api.BACKEND`로 런타임 해결. `WIKI_BACKEND` env가 단일 소스.
- **Windows 출력 주의:** `print()`에서 em-dash(`—`, U+2014) 사용 금지 → cp949 UnicodeEncodeError. 하이픈(`-`) 사용.

---

## 테스트 방법

```bash
# 빌드 파이프라인
python wiki_builder/orchestrate.py --phase plan --backend claude
python wiki_builder/orchestrate.py --phase post_plan --backend claude
python wiki_builder/orchestrate.py --phase generate --backend claude
python wiki_builder/orchestrate.py --phase link --backend claude
python wiki_builder/orchestrate.py --phase all --backend claude

# 질의
python wiki_builder/orchestrate.py --phase query --question "PUSCH scrambling 절차는?" --backend claude

# Lint
python wiki_builder/orchestrate.py --phase lint --backend claude

# 대화형 Chat REPL
python wiki_builder/orchestrate.py --phase chat --backend claude

# sdmAnalyzer 연동 서버
python wiki_builder/orchestrate.py --phase server --backend gptoss --api-key XXX

# gpt-oss 전체 실행
python wiki_builder/orchestrate.py --phase all --backend gptoss \
  --api-key XXX --knox-id XXX --ad-id XXX
```
