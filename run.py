"""
LLMWiki 실행 진입점

사용법:
  python run.py plan
  python run.py generate
  python run.py generate --workers 3
  python run.py link
  python run.py evaluate
  python run.py all
  python run.py query --question "PUSCH scrambling 절차는?"
  python run.py lint
  python run.py chat

백엔드 (환경변수 또는 --backend):
  WIKI_BACKEND=gemini   (기본값)
  WIKI_BACKEND=claude
  WIKI_BACKEND=gptoss

소스 경로 (환경변수로 변경 가능):
  WIKI_SOURCES_DIR=./sources   (기본값)
  WIKI_OUTPUT_DIR=./wiki       (기본값)
  WIKI_PLAN_PATH=./plan.json   (기본값)
"""

import sys
import os
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# .env 자동 로드
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

# 기본 백엔드: gemini
if "WIKI_BACKEND" not in os.environ:
    os.environ["WIKI_BACKEND"] = "gemini"

# orchestrate.py의 main() 재사용 — sys.argv 그대로 전달
# run.py plan → orchestrate.py --phase plan 으로 변환
if len(sys.argv) >= 2 and not sys.argv[1].startswith("--"):
    sys.argv = [sys.argv[0], "--phase"] + sys.argv[1:]

# --backend 미지정 시 gemini 기본값 주입
if "--backend" not in sys.argv:
    sys.argv += ["--backend", os.getenv("WIKI_BACKEND", "gemini")]

# 경로 환경변수 적용 (orchestrate.py 상수 override)
import wiki_builder.orchestrate as _orch
if "WIKI_SOURCES_DIR" in os.environ:
    _orch.SOURCES_DIR = Path(os.environ["WIKI_SOURCES_DIR"])
if "WIKI_OUTPUT_DIR" in os.environ:
    _orch.WIKI_DIR = Path(os.environ["WIKI_OUTPUT_DIR"])
if "WIKI_PLAN_PATH" in os.environ:
    _orch.PLAN_PATH = Path(os.environ["WIKI_PLAN_PATH"])

_orch.main()
