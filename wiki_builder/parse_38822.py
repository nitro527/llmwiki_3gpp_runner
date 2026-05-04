"""
parse_38822.py — TS 38.822 UE Feature List 파서

feature_priority.json 생성 (독립 실행 또는 orchestrate에서 자동 호출):
[
  {
    "index": "0-1",
    "release": 15,
    "work_item": "Layer-1 UE features",
    "category": "Waveform, modulation...",
    "feature_group": "CP-OFDM waveform for DL and UL",
    "components": "1) CP-OFDM for DL ...",
    "prerequisites": ["0-1", "1-1"],   # 파싱된 선행 feature index 목록
    "prerequisites_raw": "0-1, 1-1",
    "mandatory": "mandatory_always"    # mandatory_always|mandatory|optional|conditional|unknown
  }
]

standalone: python wiki_builder/parse_38822.py [docx_path] [output_json]
"""

import json
import re
import sys
from pathlib import Path


def _normalize_mandatory(raw: str) -> str:
    t = raw.lower().strip()
    if not t:
        return "unknown"
    if t.startswith("mandatory without"):
        return "mandatory_always"
    if t.startswith("mandatory with"):
        return "mandatory"
    if "mandatory" in t and "optional" in t:
        return "conditional"
    if t.startswith("optional") or "optional" in t:
        return "optional"
    if "mandatory" in t:
        return "mandatory"
    return "unknown"


def _parse_prerequisites(raw: str) -> list[str]:
    """prerequisite 컬럼 → index 목록. 예: '1-1, 1-4 or 1-5' → ['1-1', '1-4', '1-5']"""
    if not raw.strip():
        return []
    # "in Table X.Y-Z" 제거
    raw = re.sub(r'in\s+Table\s+[\d.\-]+', '', raw)
    # index 패턴 추출: 숫자-숫자 (ex: 1-1, 2-12, 10-3a)
    return re.findall(r'\b\d+[-–]\d+\w*', raw)


def parse_feature_list(docx_path: str) -> list[dict]:
    """38.822 docx에서 feature list를 release 정보와 함께 추출."""
    from docx import Document
    from docx.text.paragraph import Paragraph

    doc = Document(docx_path)

    # ── 1단계: body 순회하며 테이블별 (release, work_item) 태깅 ──
    current_release = 0
    current_work_item = ""
    table_meta: list[tuple[int, str]] = []  # [(release, work_item), ...]

    body = doc.element.body
    for child in body:
        tag = child.tag.split('}')[-1]
        if tag == 'p':
            p = Paragraph(child, doc)
            text = p.text.strip()
            # Release 헤더 감지
            m = re.search(r'Release\s+(\d+)\s+UE feature list', text, re.IGNORECASE)
            if m:
                current_release = int(m.group(1))
                current_work_item = ""
                continue
            # Work item 테이블 제목 감지 (예: "Table 5.1.3-1: Layer-1 feature list for NR_L1enh_URLLC")
            m2 = re.search(r'Table\s+[\d.\-]+:\s*(.+)', text)
            if m2:
                current_work_item = m2.group(1).strip()[:80]
        elif tag == 'tbl':
            table_meta.append((current_release, current_work_item))

    # ── 2단계: 각 테이블에서 feature 추출 ──
    features = []
    seen: set[str] = set()

    for table_idx, table in enumerate(doc.tables):
        if table_idx >= len(table_meta):
            break
        release, work_item = table_meta[table_idx]
        if release == 0:
            continue

        rows = table.rows
        if len(rows) < 2:
            continue

        header_cells = [c.text.strip().lower() for c in rows[0].cells]
        if "index" not in header_cells:
            continue

        try:
            idx_col = header_cells.index("index")
        except ValueError:
            continue

        feat_col = next(
            (i for i, h in enumerate(header_cells) if "feature group" in h), None
        )
        if feat_col is None:
            continue

        mand_col = len(rows[0].cells) - 1
        comp_col = feat_col + 1 if feat_col + 1 < len(rows[0].cells) - 1 else feat_col
        cat_col = 0
        # prerequisite 컬럼: "prerequisite" 포함하는 헤더
        prereq_col = next(
            (i for i, h in enumerate(header_cells) if "prerequisite" in h), None
        )
        field_name_col = next(
            (i for i, h in enumerate(header_cells) if "field name" in h), None
        )

        for row in rows[1:]:
            cells = row.cells
            if len(cells) <= mand_col:
                continue

            index = cells[idx_col].text.strip()
            feature_group = " ".join(cells[feat_col].text.split())  # 줄바꿈/다중공백 정리
            if not index or not feature_group:
                continue
            if index in seen:
                continue
            seen.add(index)

            category = cells[cat_col].text.strip() if cat_col < len(cells) else ""
            components = cells[comp_col].text.strip() if comp_col < len(cells) else ""
            mandatory_raw = cells[mand_col].text.strip()
            prereq_raw = cells[prereq_col].text.strip() if prereq_col is not None else ""
            field_name_raw = cells[field_name_col].text.strip() if field_name_col is not None else ""
            # n/a 또는 빈 값은 None으로
            field_name = field_name_raw if field_name_raw and field_name_raw.lower() != "n/a" else None

            features.append({
                "index": index,
                "release": release,
                "work_item": work_item,
                "category": category[:120],
                "feature_group": feature_group[:200],
                "field_name": field_name,   # TS 38.331 capability field 이름 (검색 키워드)
                "components": components[:300],
                "prerequisites": _parse_prerequisites(prereq_raw),
                "prerequisites_raw": prereq_raw[:120],
                "mandatory": _normalize_mandatory(mandatory_raw),
            })

    return features


# ──────────────────────────────────────────────
# 그룹핑 유틸
# ──────────────────────────────────────────────

def _page_name_from_category(category: str) -> str:
    """category 문자열 → features/ 페이지명 (snake_case)."""
    # 앞의 번호 제거: "0. Waveform..." → "Waveform..."
    name = re.sub(r'^\d+\.\s*', '', category.strip())
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'[\s/-]+', '_', name.strip())
    return name[:80] or "Unknown"


MAX_FEATURES_PER_PAGE = 20  # 초과 시 prereq root별 분리


def build_feature_groups(features: list[dict]) -> list[dict]:
    """
    category 기준 1차 그룹핑 + category 내 prereq 트리로 정렬.
    - 20개 초과 category → category 내 prereq root별 서브그룹 분리
    - cross-category prereq는 링크로 처리 (합치지 않음)

    반환: [
      {
        "page_name": "PUSCH_Transmission_Features",
        "category": "PUSCH",
        "releases": [15, 16],
        "features": [feat_dict, ...],
        "cross_category_prereqs": [{"index": "2-1", "feature_group": "..."}],
      }
    ]
    """
    from collections import defaultdict

    by_index = {f["index"]: f for f in features}

    # ── 1단계: category별로 묶기 ──
    cat_groups: dict[str, list] = defaultdict(list)
    for f in features:
        cat_groups[f["category"]].append(f)

    groups = []
    for category, members in cat_groups.items():
        member_indices = {f["index"] for f in members}

        # cross-category prereq 수집
        cross_prereqs: set[str] = set()
        for f in members:
            for p in f["prerequisites"]:
                if p not in member_indices and p in by_index:
                    cross_prereqs.add(p)
        cross_list = [
            {"index": idx, "feature_group": by_index[idx]["feature_group"]}
            for idx in sorted(cross_prereqs)
        ]

        # 크기 초과 → prereq root별 서브그룹 분리
        if len(members) > MAX_FEATURES_PER_PAGE:
            sub_groups = _split_by_root(members, member_indices, by_index)
        else:
            sub_groups = [(None, members)]  # (root_feat, members)

        for root_feat, sub in sub_groups:
            sorted_sub = _topo_sort(sub, {f["index"] for f in sub})
            releases = sorted(set(f["release"] for f in sorted_sub))
            # 페이지명: root feature 이름 우선, 없으면 category명
            if root_feat:
                page_name = _page_name_from_category(root_feat["feature_group"])
            else:
                page_name = _page_name_from_category(category)

            groups.append({
                "page_name": page_name,
                "category": category,
                "releases": releases,
                "features": sorted_sub,
                "cross_category_prereqs": cross_list,
            })

    groups.sort(key=lambda g: (g["releases"][0], -len(g["features"])))
    return groups


def _split_by_root(
    members: list[dict],
    member_indices: set[str],
    by_index: dict,
) -> list[tuple[dict | None, list[dict]]]:
    """
    category 내 prereq root별로 서브그룹 분리.
    반환: [(root_feat, [member, ...]), ...]
    """
    def find_cat_root(idx: str, visited: set) -> str:
        if idx in visited:
            return idx
        visited.add(idx)
        feat = by_index.get(idx)
        if not feat:
            return idx
        for p in feat["prerequisites"]:
            if p in member_indices:
                return find_cat_root(p, visited)
        return idx

    from collections import defaultdict
    root_map: dict[str, str] = {}
    for f in members:
        root_map[f["index"]] = find_cat_root(f["index"], set())

    sub_raw: dict[str, list] = defaultdict(list)
    for f in members:
        sub_raw[root_map[f["index"]]].append(f)

    # 작은 서브그룹들 합치기 (MAX 이하로), root_feat 기록
    result: list[tuple[dict | None, list[dict]]] = []
    current: list[dict] = []
    current_root: dict | None = None
    for root_idx, sub in sorted(sub_raw.items()):
        root_feat = by_index.get(root_idx)
        if len(current) + len(sub) > MAX_FEATURES_PER_PAGE and current:
            result.append((current_root, current))
            current = []
            current_root = root_feat
        elif current_root is None:
            current_root = root_feat
        current.extend(sub)
    if current:
        result.append((current_root, current))

    return result if result else [(None, members)]


def _topo_sort(members: list[dict], member_indices: set[str]) -> list[dict]:
    """category 내 prereq 기반 위상 정렬. R15 root → R16 children 순서."""
    # 간단한 위상 정렬: prereq 없는 것 먼저, 있는 것 나중
    no_prereq = [f for f in members if not any(p in member_indices for p in f["prerequisites"])]
    has_prereq = [f for f in members if any(p in member_indices for p in f["prerequisites"])]

    # release, index 순으로 2차 정렬
    no_prereq.sort(key=lambda f: (f["release"], f["index"]))
    has_prereq.sort(key=lambda f: (f["release"], f["index"]))
    return no_prereq + has_prereq


# ──────────────────────────────────────────────
# 런타임 유틸 (generate.py에서 import)
# ──────────────────────────────────────────────

_STOP_WORDS = {
    "the", "for", "and", "with", "in", "of", "to", "a", "an", "is", "based",
    "type", "by", "from", "on", "at", "or", "be", "can", "not", "that", "this",
    "ue", "nr", "lte", "5g", "3gpp", "release", "rel",
}


def _keywords_from_text(text: str) -> list[str]:
    text = re.sub(r'[_/\\.]', ' ', text)
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    words = re.findall(r'[A-Za-z가-힣]{2,}', text)
    return [w.lower() for w in words if w.lower() not in _STOP_WORDS]


def find_relevant_features(
    features: list[dict],
    keywords: list[str],
    top_n: int = 12,
) -> list[dict]:
    kw_set = set(keywords)
    scored = []
    for f in features:
        search_text = (
            f["feature_group"] + " " + f["category"] + " " + f["components"]
        ).lower()
        score = sum(1 for k in kw_set if k in search_text)
        if score > 0:
            scored.append((score, f))
    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored[:top_n]]


_MANDATORY_LABEL = {
    "mandatory_always": "필수(항상)",
    "mandatory": "필수(cap)",
    "optional": "선택",
    "conditional": "조건부",
    "unknown": "?",
}


def format_feature_hint(features: list[dict]) -> str:
    if not features:
        return "(관련 UE feature 정보 없음)"
    lines = []
    for f in features:
        label = _MANDATORY_LABEL.get(f["mandatory"], "?")
        lines.append(f"- [{label}] {f['index']}: {f['feature_group']}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 독립 실행
# ──────────────────────────────────────────────

if __name__ == "__main__":
    docx = sys.argv[1] if len(sys.argv) > 1 else "sources/3gpp_ref/38822-i00.docx"
    out = sys.argv[2] if len(sys.argv) > 2 else "feature_priority.json"

    print(f"파싱 중: {docx}")
    feats = parse_feature_list(docx)
    print(f"추출된 feature: {len(feats)}개")

    from collections import Counter
    rel_cnt = Counter(f["release"] for f in feats)
    for r, c in sorted(rel_cnt.items()):
        print(f"  Rel-{r}: {c}개")

    with open(out, "w", encoding="utf-8") as f:
        json.dump(feats, f, ensure_ascii=False, indent=2)
    print(f"저장 완료: {out}")
