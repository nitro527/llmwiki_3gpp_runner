# AGENTS.md — LLMWiki 런타임 에이전트 행동 지침

> **이 파일의 독자**: `python wiki_builder/orchestrate.py`로 실행되는 **런타임 에이전트 프로그램**.
>
> Karpathy 패턴에서 말하는 "schema" — 에이전트가 wiki를 어떻게 구조화하고,
> 어떤 규칙을 따르고, 어떤 워크플로를 수행할지 정의한다.
> 원문: `KARPATHY_LLMWIKI.md`

---

## Part 1 — Wiki 목표 및 불변 기준 ⛔ 수정 불가

> 이 섹션은 wiki의 존재 이유와 품질 기준을 정의한다.
> **어떤 자율적 행동으로도 이 섹션을 수정하지 말 것.**
> (Evaluator의 자동 개선 대상도 아님)

### 목표

5G NR PHY 스펙(3GPP TS 38.2xx)으로부터 LLM이 모뎀 로그 분석 시 참조할 수 있는
고품질 도메인 지식베이스(wiki)를 자동 구축·유지·조회한다.

- 대상 사용자: 5G NR PHY 펌웨어 엔지니어
- 핵심 차이: RAG(매번 재발견)가 아니라 **누적되는 wiki** (한 번 컴파일, 계속 최신화)
- wiki는 LLM이 로그 이상 징후 분석 시 인과관계·판단기준·스펙근거를 조회하는 영구 지식

### 품질 기준 (8점 만점, 7점 이상 합격)

| 항목 | 점수 |
|------|------|
| 필수 섹션 구조 준수 | 2점 |
| 기술 용어 번역 없음 (원문 그대로) | 1점 |
| 관련 개념에 관계 타입 있음 | 1점 |
| 스펙 근거 명시 | 1점 |
| hallucination 없음 | 2점 |
| 상세 설명이 소스 원문 기반 | 1점 |

### 에이전트가 절대 수정하지 말아야 할 파일

| 파일 | 이유 |
|------|------|
| `wiki_builder/orchestrate.py` | Orchestrator LLM 에이전트 — 에이전트가 자율적으로 tool 호출 로직을 수정하지 말 것 |
| `AGENTS.md Part 1` (이 섹션) | wiki 목표와 평가 기준 — 불변 |
| `KARPATHY_LLMWIKI.md` | 원문 보존 — 읽기만 허용 |
| `sources/` | 원본 소스 — 읽기만 허용 |

---

## Part 2 — 방법론 ✏️ Evaluator + 사람 승인 후 전체 재작성 가능

> 이 섹션은 wiki 작성 방식과 에이전트 행동 절차를 정의한다.
> 자동 품질 개선(Phase 4: Evaluate)이 **사람의 승인을 받은 경우에만** 전체 재작성 가능.
> **단순 추가·부분 수정 금지 — 변경 시 항상 전체 재작성.**

Evaluator가 수정할 수 있는 파일:
- `wiki_builder/prompts.py` — 프롬프트 개선 시 전체 재작성
- `AGENTS.md Part 2` (이 섹션) — 방법론 개선 시 전체 재작성

---

### 멀티에이전트 구조

이 시스템은 **Orchestrator LLM이 tool을 통해 각 Phase(서브에이전트)를 호출**하는 구조로 동작한다.

```
Orchestrator LLM (에이전트)
  ├── tool: run_plan()       Phase 1:   Planner    — 소스 청킹 → wiki 페이지 목록 계획
  ├── tool: run_post_plan()  Phase 1.5: Post-Plan  — plan.json 검증 (중복 섹션, 의미적 불일치)
  ├── tool: run_generate()   Phase 2:   Generator  — 페이지별 wiki 문서 생성 (병렬)
  │                             └── Quality Checker — 생성 직후 품질 평가
  ├── tool: run_evaluate()   Phase 4:   Evaluator  — 불합격 원인 분석 → 개선안 → 사람 승인  ← Generate 직후 실행
  ├── tool: run_link()       Phase 3:   Linker     — 역방향 링크 보완                        ← Evaluate 완료 후 실행
  ├── tool: run_lint()       Phase 6:   Lint       — wiki 건강 검진
  ├── tool: run_chat()       Chat REPL             — 터미널 대화형 인터페이스
  └── tool: run_server()     Server                — JSON stdio 서버 (sdmAnalyzer 연동)

query.py  ← 예외: tool 방식 아님. 2-step stateless 호출로 독립 동작.
```

**Orchestrator 역할:**
- 사용자 요청(--phase 인자 또는 런타임 상황)에 따라 어떤 tool을 호출할지 LLM이 판단
- Phase 간 의사결정 (예: Generate 불합격 발생 → Evaluate 호출 여부) 을 Python 하드코딩이 아닌 LLM이 결정
- 각 tool 호출은 독립적 — Orchestrator가 결과를 받아 다음 action 결정

**서브에이전트(각 Phase) 원칙:**
- 각 Phase는 **독립 LLM 호출** — messages 배열 누적 없음
- 페이지당 LLM 1회 호출 (컨텍스트 오염 방지)
- `call_simple(system, user)` 형태의 stateless 호출만 사용
- agent 루프 / messages 배열 누적 방식 사용 금지

---

### Phase 1: Planner

- 소스 파일을 청크 단위로 읽어 생성할 wiki 페이지 목록 계획
- 각 페이지가 참조할 섹션 번호 기록 (예: 38211 §6.3.1)
- 결과를 `plan.json`에 저장 (`planned_sources` 필드에 처리 완료 소스 기록)
- **재실행 동작**: plan.json의 `planned_sources`와 현재 소스 파일 비교 → 새 소스만 증분 플래닝
- **증분 저장**: 소스 파일 하나 처리 완료 시마다 plan.json 저장 (중간 crash 복구 지원)
- **멀티소스**: 동일 path가 여러 소스에서 등장하면 sources 배열에 머지
- **existing_pages 전달**: LLM에 `path: description` 형태로 전달하여 기존 범위 인지
- **description 업데이트**: 멀티소스 머지 시 새 description이 더 넓으면 교체

**컨텍스트 관리:**
- 청크 전체를 LLM에 전달하지 말 것
- 소스 파일 전체를 LLM에 전달하지 말 것

---

### Phase 1.5: Post-Plan

- Plan 완료 후 Generate 시작 전 plan.json 품질 검증
- **코드 검증** (LLM 불필요):
  - 동일 파일의 동일 섹션이 여러 페이지에 중복 배정된 경우 감지 및 로그
- **LLM 검증** (페이지 단위):
  - 페이지 path/description과 배정 섹션의 의미적 불일치 감지
  - 불일치 시 해당 소스 항목 제거 또는 올바른 페이지로 이동
  - 검증 결과 plan.json에 반영 후 저장
- `post_plan` 플래그가 plan.json에 기록됨 → 재실행 시 스킵

---

### Phase 2: Generator

- plan.json 기반으로 페이지별 wiki 문서 생성
- 입력: plan에 명시된 섹션 내용 + page path 목록 (내용 없이 path만)
- 병렬 처리: `ThreadPoolExecutor`, `max_workers=1` (기본값; `--workers N`으로 변경 가능)
- 같은 path에 동시 write 없음 (페이지당 LLM 1회이므로 충돌 없음)
- 생성 직후 Quality Checker가 품질 평가 → 불합격 시 Evaluator 호출
- `plan.json`의 `generated=True` 플래그로 재실행 시 완료 페이지 스킵

**Hallucination 감지 (저장 전 필수 체크):**
```
3어절 이상 동일 구절이 5회 이상 반복 → hallucination으로 판정, 저장 거부
예: "시대로 다른 시대로 다른 시대로..." → 차단
```
- 차단 후 LLM에게 재작성 요청
- 3회 연속 차단 시 해당 페이지 스킵, 로그 기록

**Generate 완료 후:** `update_index()` 자동 호출

---

### Phase 3: Linker

- Python이 전체 wiki 읽고 링크 맵 구성
- 역방향 링크 누락 감지 → LLM에게 추가 요청
- LLM에게 전달: 자기 파일 내용 + inbound 링크 목록만
- `plan.json`의 `linked=True` 플래그로 재실행 시 완료 페이지 스킵

---

### Phase 4: Evaluator

- 불합격 문서 원인 분석 (LLM 호출)
- 개선안 도출: `wiki_builder/prompts.py` 또는 `AGENTS.md Part 2` 수정안 생성
- 개선안으로 검증 재실행
- 보고서 생성 → **사람 컨펌 대기 (터미널 입력)**
- 승인 후 해당 파일 수정 및 빌드 재개
- 최대 5회 반복 후 개선 없으면 사람에게 알림

**⚠️ 실행 순서 필수: Evaluate → Link**

Evaluate가 페이지를 재생성하면 Link가 추가한 역방향 링크가 사라진다.
따라서 **반드시 Evaluate를 먼저 완료한 후 Link를 실행**할 것.
Link → Evaluate 순서는 이중 작업을 유발하므로 금지.

---

### Phase 5: Query

**2-step stateless** (LLM 호출 2회, messages 누적 없음):
1. `wiki/index.md` + 질문 → 관련 페이지 경로 목록 (JSON) 반환
2. 선택된 페이지 내용 + 질문 → 답변 + 출처 반환

- 페이지 선택 최대 5개 (컨텍스트 과부하 방지)
- 답변 저장: `wiki/query/YYYY-MM-DD_slug.md`
- `wiki/log.md`에 `## [날짜] query | 질문` 형식으로 기록

**실행 방법 두 가지:**
- `--phase query --question "..."` → 직접 실행 (스크립트/자동화용)
- Chat REPL 또는 Orchestrator → `run_query` tool로 자동 호출

---

### Phase 6: Lint

**Python이 직접 계산:**
- 고아 페이지 (inbound 링크 0개)
- 역방향 링크 누락
- 존재하지 않는 `[[링크]]` (`[[X]]` 있는데 X.md 없음)

**LLM 호출** (페이지 5개 묶음 단위):
- 내용 모순 감지
- 오래된 주장 감지
- 데이터 공백 제안

- LLM에게 전체 wiki를 한 번에 전달하지 말 것
- 리포트: `wiki/lint_YYYY-MM-DD.md`
- `wiki/log.md`에 lint 결과 기록

---

### Chat REPL

- **stateless per turn**: 각 입력마다 독립 Orchestrator 실행, 대화 history 누적 없음
- 사용자 입력을 **Orchestrator LLM에 직접 전달** — Orchestrator가 tool 선택 판단
- 예: "PUSCH가 뭐야?" → Orchestrator → `run_query` 호출
- 예: "generate 시작해" → Orchestrator → `run_generate` 호출
- `/exit` 또는 `exit` 입력 시 종료

---

### Server 모드 (sdmAnalyzer 연동)

- `--phase server` → stdin 루프, **stdout은 JSON 전용** — print/log를 stdout에 출력 금지
- 모든 로그는 `logging` (→ stderr 또는 파일)으로만 출력
- 한 줄 = JSON 한 객체 (line-delimited JSON), `\n`으로 구분
- 각 요청 처리는 독립 stateless — 이전 요청 context 누적 없음
- 요청 `id` 필드를 응답에 그대로 echo
- stdin EOF 수신 시 정상 종료 (SystemExit 아닌 return)
- 응답 필수 필드: `id`, `status` (`"ok"` 또는 `"error"`)

**WikiClient (sdmAnalyzer 측):**
- `wiki_client.py`는 sdmAnalyzer가 import하는 파일
- 표준 라이브러리만 사용 (subprocess, threading, queue, json, uuid)
- anthropic, requests 등 wiki 의존성 import 금지
- `query_async`는 GUI 메인 스레드를 블로킹하면 안 됨 → threading.Thread 사용
- 프로세스 재시작 시 진행 중인 요청은 error 응답으로 처리

---

### 재개 메커니즘

- `plan.json`이 체크포인트 — 절대 삭제하지 말 것
- `generated=True`인 페이지는 재실행 시 스킵
- `linked=True`인 페이지는 재실행 시 스킵

---

### 에러 처리

- LLM 호출 실패: 재시도 최대 3회, exponential backoff (5→10→20초)
- 파싱 실패: 재시도 최대 3회
- 3회 모두 실패: 해당 페이지/청크 스킵, 로그 기록
- 429 Rate limit: 65초 대기 후 재시도 (재시도 횟수 카운트 제외 — 무한 재시도)
- 예외는 raise하지 말고 로그 기록 후 계속 진행

---

### index.md / log.md 관리

- Generate 완료 후 `update_index()` 자동 호출
- Query / Lint 실행 시 `append_log()` 자동 호출
- log.md 형식: `## [YYYY-MM-DD] {type} | {title}`

---

### wiki 파일 작성 규칙

#### 디렉토리 분류

| 디렉토리 | 수록 내용 | 예시 |
|---------|---------|------|
| `wiki/entities/` | 실제 존재하는 것 | PUSCH, HARQ, BWP, DMRS |
| `wiki/concepts/` | 동작·절차 — **파일명에 반드시 동작어 포함** | PUSCH_Scrambling, HARQ_Retransmission |
| `wiki/features/` | UE capability feature group (TS 38.822) — category 단위 1페이지 | PUSCH_Transmission_Features |
| `wiki/internal/` | 팀 경험, 현장 관찰 | 특이 로그 패턴, 실측 결과 |

concepts/ 금지: `concepts/PUSCH.md` → 허용: `concepts/PUSCH_Scrambling.md`
features/ 생성: `run_plan_features` → `run_generate_features` → `run_link` (기존 파이프라인과 별도 실행)

#### 필수 섹션 (이 순서, 이 이름 정확히)

```markdown
# [페이지명]

## 정의

## 요약

## 상세 설명

## 인과 관계

## 관련 개념

## 스펙 근거

## 소스
```

비표준 섹션명 사용 금지: `## Overview`, `## References`, `## 로그 필드` 등

#### 관계 타입

관련 개념 섹션의 모든 링크에 관계 타입 필수:
```markdown
- [[PUSCH]] (part_of)
- [[HARQ]] (affects)
- [[BWP]] (depends_on)
```

허용 관계 타입: `affects` / `depends_on` / `triggers` / `part_of` / `similar_to`

관계 타입 없는 링크 금지: `- [[Slot]]` (X) → `- [[Slot]] (part_of)` (O)

#### 절대 금지

| 금지 | 대안 |
|------|------|
| `**PUSCH**` (bold 강조) | `[[PUSCH]]` (wikilink) |
| 영어 기술 용어 한국어 번역 | 원문 그대로 (PUSCH, HARQ, Scrambling 등) |
| 비표준 섹션명 | 위 필수 섹션 구조 준수 |
| 관계 타입 없는 관련 개념 링크 | `(관계타입)` 필수 |
| 소스에 없는 내용 지어내기 | 소스 근거 있는 내용만 |

#### 목표 품질 샘플

`sample.md` 참조. 사람이 직접 작성한 기준 문서. 생성 시 이 품질을 목표로 한다.
