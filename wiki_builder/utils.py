import json
import re
from pathlib import Path


def save_plan(plan: dict, plan_path) -> None:
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)


def load_json_safe(path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def extract_json_from_llm(raw: str) -> dict | list | None:
    """LLM 응답에서 JSON 추출. ```json...``` → {.*} 순으로 시도."""
    m = re.search(r'```json\s*([\s\S]+?)\s*```', raw)
    text = m.group(1) if m else raw
    text = re.sub(r'```\w*\s*', '', text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None
