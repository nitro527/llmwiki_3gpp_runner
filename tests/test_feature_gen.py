"""
test_feature_gen.py — features/ 페이지 3개 생성 테스트 (gemini-3-flash-preview)
"""
import sys, io, os, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = __import__('pathlib').Path(__file__).parent
sys.path.insert(0, str(ROOT))

for line in open(ROOT / '.env', encoding='utf-8'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, _, v = line.partition('=')
        if k.strip() and v.strip() and k.strip() not in os.environ:
            os.environ[k.strip()] = v.strip()

import warnings
warnings.filterwarnings('ignore')
import google.generativeai as genai
genai.configure(api_key=os.environ['GEMINI_API_KEY'])

from wiki_builder.prompt_loader import load_prompt
FEATURE_GENERATOR_SYSTEM, FEATURE_GENERATOR_USER = load_prompt("feature_generator")
from wiki_builder.parse_38822 import _MANDATORY_LABEL

MODEL = "models/gemini-3-flash-preview"

def call_gemini(system, user):
    model = genai.GenerativeModel(model_name=MODEL, system_instruction=system)
    resp = model.generate_content(user, generation_config={"temperature": 0.3, "max_output_tokens": 4096})
    return resp.text

# plan.json 로드
plan = json.loads((ROOT / 'plan.json').read_text(encoding='utf-8'))
pages = [p for p in plan['pages'] if p['path'].startswith('features/') and not p.get('generated')]
wiki_page_list = "\n".join(p['path'] for p in plan['pages'])

# 의미있어 보이는 3개 선택
targets = [p for p in pages if p['path'] in [
    'features/Basic_PUSCH_transmission.md',
    'features/Basic_PDSCH_reception.md',
    'features/Initial_access_and_mobility.md',
]]

out_dir = ROOT / 'wiki' / 'features'
out_dir.mkdir(parents=True, exist_ok=True)

for page in targets:
    g = page['feature_group']
    print(f"\n--- 생성 중: {page['path']} ({len(g['features'])}개 feature) ---")

    # Feature 목록 테이블
    lines = ["| Index | Feature | Field name (TS 38.331) | Rel | Status |",
             "|-------|---------|----------------------|-----|--------|"]
    for f in g['features']:
        label = _MANDATORY_LABEL.get(f['mandatory'], '?')
        field = f.get('field_name') or 'n/a'
        lines.append(f"| {f['index']} | {f['feature_group'][:55]} | {field} | Rel-{f['release']} | {label} |")
    feature_table = "\n".join(lines)

    cross = g.get('cross_category_prereqs', [])
    cross_summary = "\n".join(f"- {c['index']}: {c['feature_group']}" for c in cross) if cross else "(없음)"

    user_msg = FEATURE_GENERATOR_USER.format(
        page_name=g['page_name'],
        feature_table=feature_table,
        cross_prereq_summary=cross_summary,
        wiki_page_list=wiki_page_list,
    )

    content = call_gemini(FEATURE_GENERATOR_SYSTEM, user_msg)
    out_path = out_dir / f"{g['page_name']}.md"
    out_path.write_text(content, encoding='utf-8')
    page['generated'] = True
    print(f"저장 완료: {out_path}")
    print(content[:500])
    print("...")

(ROOT / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
print("\n=== 완료 ===")
