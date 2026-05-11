"""
conftest.py — pytest 전역 설정

실제 LLM API를 호출하는 통합 테스트 스크립트는 자동 수집에서 제외한다.
test_feature_gen.py, test_feature_pages.py는 pytest용이 아니라
직접 실행하는 스크립트이므로 collect_ignore에 등록한다.
"""

import sys
from pathlib import Path

# 프로젝트 루트(tests/ 의 부모)를 sys.path에 등록
sys.path.insert(0, str(Path(__file__).parent.parent))

collect_ignore = [
    "test_feature_gen.py",
    "test_feature_pages.py",
]
