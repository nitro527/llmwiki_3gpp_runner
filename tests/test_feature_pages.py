"""
test_feature_pages.py — 38.822 feature group wiki 페이지 생성 테스트

R15 + R16 feature를 그룹핑하고, 샘플 그룹 3개를 Claude Sonnet으로 생성.

실행: python test_feature_pages.py
"""

import json
import os
import sys
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# .env 로드
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() and v.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()

from wiki_builder.parse_38822 import parse_feature_list, build_feature_groups, _MANDATORY_LABEL


# ──────────────────────────────────────────────
# 프롬프트
# ──────────────────────────────────────────────

FEATURE_PAGE_SYSTEM = """당신은 5G NR UE capability 전문가입니다.
TS 38.822 feature 데이터를 바탕으로 wiki 페이지를 작성합니다.

## 필수 섹션 (이 순서, 이 섹션명 정확히)
```
# [페이지명]
## 정의
## Feature 목록
## Release 진화
## 선행 Feature
## 관련 절차
## 스펙 근거
## 소스
```

## 작성 규칙
- ## Feature 목록 섹션: 아래 마크다운 테이블 형식 그대로 사용 (LLM이 수정 금지)
- ## 선행 Feature: 데이터에 있는 prerequisite만 [[wikilink]]로 작성. 없으면 "(없음)"
- ## 관련 절차: feature 이름에서 유추되는 스펙 절차를 [[wikilink]]로 연결
- ## Release 진화: R15 base → R16 확장 흐름을 1~3문장으로 서술
- 영어 기술 용어 번역 금지
- hallucination 금지 — 데이터에 없는 내용 지어내지 말 것
- ## 정의는 2~4문장, 이 feature group이 UE에게 무엇을 가능하게 하는지 서술
"""

FEATURE_PAGE_USER = """\
다음 feature group 데이터로 wiki 페이지를 작성하세요.

## 페이지 경로
features/{page_name}.md

## Feature 목록 (그대로 삽입할 테이블)
{feature_table}

## Prerequisite 요약
{prereq_summary}

## 전체 wiki 페이지 목록 (관련 절차 링크용)
{wiki_page_list}

위 형식에 맞춰 wiki 페이지를 작성하세요. JSON이나 코드블록 없이 마크다운 그대로 출력하세요.
"""


# ──────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────

def make_page_name(group: dict) -> str:
    """group_name → snake_case 페이지명."""
    name = group["group_name"]
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'[\s-]+', '_', name.strip())
    return name[:60]


def make_feature_table(group: dict) -> str:
    """feature group → 마크다운 테이블."""
    lines = ["| Index | Feature | Rel | Status |",
             "|-------|---------|-----|--------|"]
    for f in group["features"]:
        label = _MANDATORY_LABEL.get(f["mandatory"], "?")
        lines.append(
            f"| {f['index']} | {f['feature_group'][:60]} | Rel-{f['release']} | {label} |"
        )
    return "\n".join(lines)


def make_prereq_summary(group: dict, by_index: dict) -> str:
    """그룹 내 prerequisite 관계 요약."""
    lines = []
    for f in group["features"]:
        if f["prerequisites"]:
            prereq_names = []
            for p in f["prerequisites"]:
                pf = by_index.get(p)
                prereq_names.append(f"{p}({pf['feature_group'][:30] if pf else '?'})")
            lines.append(f"- {f['index']} {f['feature_group'][:40]} → 선행: {', '.join(prereq_names)}")
    return "\n".join(lines) if lines else "(선행 feature 없음)"


def call_claude_sonnet(system: str, user: str) -> str:
    """Gemini로 LLM 호출."""
    import google.generativeai as genai
    genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
    model = genai.GenerativeModel(
        model_name=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-lite"),
        system_instruction=system,
    )
    resp = model.generate_content(
        user,
        generation_config={"temperature": 0.3, "max_output_tokens": 4096},
    )
    return resp.text


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def main():
    print("=== 38.822 Feature Group 페이지 생성 테스트 ===\n")

    # 1. feature 로드
    fp = ROOT / "feature_priority.json"
    if fp.exists():
        features = json.loads(fp.read_text(encoding="utf-8"))
        print(f"feature_priority.json 로드: {len(features)}개")
    else:
        print("파싱 중...")
        features = parse_feature_list(str(ROOT / "sources/3gpp_ref/38822-i00.docx"))

    # R15 + R16만 필터
    features_r15_r16 = [f for f in features if f["release"] in (15, 16)]
    print(f"R15/R16 feature: {len(features_r15_r16)}개")

    by_index = {f["index"]: f for f in features}  # 전체 index 맵 (prereq lookup용)

    # 2. 그룹핑
    groups = build_feature_groups(features_r15_r16)
    multi_release = [g for g in groups if len(g["releases"]) > 1]
    print(f"그룹 총 {len(groups)}개, R15+R16 걸친 그룹: {len(multi_release)}개\n")

    # 3. 테스트 대상 선정: R15+R16 걸친 그룹 중 크기순 상위 3개
    test_groups = multi_release[:3]
    print("=== 테스트 그룹 ===")
    for g in test_groups:
        print(f"  [{g['group_id']}] {g['group_name']} - {len(g['features'])}개 feature, Rel{g['releases']}")

    # 4. 기존 wiki 페이지 목록 (링크용)
    wiki_dir = ROOT / "wiki"
    wiki_pages = []
    if wiki_dir.exists():
        wiki_pages = [
            str(p.relative_to(wiki_dir)).replace("\\", "/")
            for p in wiki_dir.rglob("*.md")
        ]
    wiki_page_list = "\n".join(wiki_pages) if wiki_pages else "(wiki 페이지 없음)"

    # 5. 출력 디렉토리
    out_dir = ROOT / "wiki" / "features"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 6. 페이지 생성
    for g in test_groups:
        page_name = make_page_name(g)
        print(f"\n--- 생성 중: features/{page_name}.md ---")

        feature_table = make_feature_table(g)
        prereq_summary = make_prereq_summary(g, by_index)

        user_msg = FEATURE_PAGE_USER.format(
            page_name=page_name,
            feature_table=feature_table,
            prereq_summary=prereq_summary,
            wiki_page_list=wiki_page_list,
        )

        content = call_claude_sonnet(FEATURE_PAGE_SYSTEM, user_msg)

        out_path = out_dir / f"{page_name}.md"
        out_path.write_text(content, encoding="utf-8")
        print(f"저장: {out_path}")
        print(f"미리보기:\n{content[:400]}\n...")

    print("\n=== 완료 ===")


if __name__ == "__main__":
    main()
