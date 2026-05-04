"""
chunk_text.py — 3GPP .docx 파일 청킹 유틸리티

read_file_content(path) -> str
    .docx 파일을 읽어 텍스트 반환. 섹션 번호는 \n{번호}\t{제목} 형태로 유지.

find_chunk_boundary(text, start, min_size, max_size) -> int
    start 위치에서 [min_size, max_size] 범위 내 섹션 경계 반환.
"""

import re
import os

try:
    from docx import Document
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False


def read_file_content(path: str) -> str:
    """
    파일 읽기. .docx는 python-docx로 파싱, 나머지는 텍스트로 읽음.
    섹션 헤더 형식: \\n{번호}\\t{제목}
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".docx":
        if not HAS_DOCX:
            raise ImportError("python-docx가 설치되어 있지 않습니다: pip install python-docx")
        return _read_docx(path)
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()


def _read_docx(path: str) -> str:
    """
    .docx에서 단락을 읽고 섹션 번호 패턴(숫자.숫자\t...)을 감지해
    \\n{번호}\\t{제목} 형태의 텍스트로 변환.
    """
    doc = Document(path)
    lines = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        # 섹션 번호 패턴: "6.3.1  Physical uplink shared channel" 등
        # 탭 또는 2+ 공백으로 번호와 제목이 구분됨
        m = re.match(r'^(\d+(?:\.\d+)*)\s{2,}(.+)$', text)
        if m:
            lines.append(f"\n{m.group(1)}\t{m.group(2)}")
        else:
            # 일반 본문
            lines.append(text)

    return "\n".join(lines)


def find_chunk_boundary(text: str, start: int, min_size: int, max_size: int) -> int:
    """
    text[start:] 에서 [min_size, max_size] 범위 내 섹션 경계를 찾아 절대 위치 반환.

    섹션 경계: \\n\\d+(\\.\\d+)*\\t 패턴 (같은 레벨 이상의 헤더)
    범위 내 경계가 없으면 start + max_size 반환.
    """
    search_start = start + min_size
    search_end = start + max_size

    if search_end >= len(text):
        return len(text)

    # 섹션 헤더 패턴 검색
    pattern = re.compile(r'\n(\d+(?:\.\d+)*)\t')
    best = None

    for m in pattern.finditer(text, search_start, min(search_end + 1000, len(text))):
        pos = m.start()
        if pos >= search_start:
            best = pos
            break

    if best is not None:
        return best

    return min(start + max_size, len(text))


def chunk_file(path: str, min_size: int = 40000, max_size: int = 50000) -> list[dict]:
    """
    파일을 청크로 분할. 각 청크는 {index, text, start, end} 딕셔너리.
    """
    content = read_file_content(path)
    chunks = []
    start = 0
    idx = 0

    while start < len(content):
        end = find_chunk_boundary(content, start, min_size, max_size)
        chunk_text = content[start:end]
        if chunk_text.strip():
            chunks.append({
                "index": idx,
                "text": chunk_text,
                "start": start,
                "end": end,
            })
            idx += 1
        start = end

    return chunks


def extract_abbreviations(text: str) -> dict[str, str]:
    """
    3GPP 스펙의 약어 섹션(3.3) 파싱.
    \\n3.3\\t 두 번째 매치에서 시작, \\n4\\t까지.
    반환: {"PUSCH": "Physical Uplink Shared Channel", ...}
    """
    abbrevs = {}

    # 3.3 섹션 찾기 (두 번째 매치)
    pattern_33 = re.compile(r'\n3\.3\t')
    matches = list(pattern_33.finditer(text))
    if len(matches) < 1:
        return abbrevs

    start = matches[-1].end()

    # 4절 시작 위치
    pattern_4 = re.compile(r'\n4\t')
    m4 = pattern_4.search(text, start)
    end = m4.start() if m4 else len(text)

    section_text = text[start:end]

    # "ABBREVIATION    Expansion" 패턴
    for line in section_text.split('\n'):
        line = line.strip()
        m = re.match(r'^([A-Z][A-Z0-9\-/]{1,20})\s{2,}(.+)$', line)
        if m:
            abbrevs[m.group(1)] = m.group(2)

    return abbrevs


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python chunk_text.py <file>")
        sys.exit(1)

    path = sys.argv[1]
    chunks = chunk_file(path)
    print(f"총 {len(chunks)}개 청크")
    for c in chunks:
        print(f"  청크 {c['index']}: {c['start']}~{c['end']} ({c['end']-c['start']}자)")
        print(f"    첫 100자: {c['text'][:100].strip()!r}")
