"""
wiki_client.py — sdmAnalyzer에서 import하는 WikiClient 래퍼

표준 라이브러리만 사용 (subprocess, threading, queue, json, uuid).
llmwiki venv의 서버 프로세스를 spawn하여 JSON stdio로 통신.

사용 예시 (sdmAnalyzer 측):
    from wiki_builder.wiki_client import WikiClient

    client = WikiClient(
        python_exe=r"D:\\work\\llmwiki\\.venv\\Scripts\\python.exe",
        wiki_root=r"D:\\work\\llmwiki",
        backend="gptoss",
        api_key="...",
    )
    client.start()

    # 동기
    result = client.query("PUSCH scrambling 절차는?")
    print(result["answer"])

    # 비동기 (GUI용)
    client.query_async("PUSCH scrambling 절차는?", callback=self.on_response)

    client.stop()
"""

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60    # 초
LINT_TIMEOUT = 120      # 초


class WikiClient:
    """
    llmwiki JSON stdio 서버의 클라이언트.
    sdmAnalyzer GUI에서 사용.
    """

    def __init__(
        self,
        python_exe: str,
        wiki_root: str,
        backend: str | None = None,
        api_key: str = "",
        knox_id: str = "",
        ad_id: str = "",
        gemini_key: str = "",
    ):
        self._python_exe = python_exe
        self._wiki_root = Path(wiki_root)
        self._backend = backend or os.getenv("WIKI_BACKEND", "gemini")
        self._api_key = api_key
        self._knox_id = knox_id
        self._ad_id = ad_id
        self._gemini_key = gemini_key

        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._pending: dict[str, threading.Event] = {}   # req_id → Event
        self._results: dict[str, dict] = {}              # req_id → response
        self._callbacks: dict[str, Callable] = {}        # req_id → callback
        self._lock = threading.Lock()
        self._running = False

    # ──────────────────────────────────────────────
    # 수명 관리
    # ──────────────────────────────────────────────

    def start(self) -> None:
        """서버 프로세스 spawn 및 reader 스레드 시작."""
        orchestrate = self._wiki_root / "wiki_builder" / "orchestrate.py"
        cmd = [
            self._python_exe,
            str(orchestrate),
            "--phase", "server",
            "--backend", self._backend,
        ]
        if self._api_key:
            cmd += ["--api-key", self._api_key]
        if self._knox_id:
            cmd += ["--knox-id", self._knox_id]
        if self._ad_id:
            cmd += ["--ad-id", self._ad_id]
        if self._gemini_key:
            cmd += ["--gemini-key", self._gemini_key]

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(self._wiki_root),
        )
        self._running = True

        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="wiki-reader"
        )
        self._reader_thread.start()

        # stderr 로그 스레드
        threading.Thread(
            target=self._stderr_loop, daemon=True, name="wiki-stderr"
        ).start()

        # 생존 확인
        if not self.ping():
            logger.error("Wiki server 시작 실패")
            self.stop()
            raise RuntimeError("Wiki server 시작 실패")

        logger.info("WikiClient 시작 완료")

    def stop(self) -> None:
        """서버 프로세스 종료."""
        self._running = False
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None
        logger.info("WikiClient 종료")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ──────────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────────

    def ping(self, timeout: float = 5.0) -> bool:
        """서버 생존 확인."""
        try:
            result = self._send({"action": "ping"}, timeout=timeout)
            return result.get("status") == "pong"
        except Exception:
            return False

    def status(self, timeout: float = 10.0) -> dict:
        """wiki 상태 조회."""
        return self._send({"action": "status"}, timeout=timeout)

    def query(self, question: str, file: bool = False,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
        """wiki 질의 (동기). GUI 메인 스레드에서 호출 시 블로킹 주의."""
        return self._send(
            {"action": "query", "question": question, "file": file},
            timeout=timeout,
        )

    def query_async(self, question: str, callback: Callable[[dict], None],
                    file: bool = False) -> str:
        """
        wiki 질의 (비동기). callback(result_dict)으로 응답 전달.
        GUI 메인 스레드를 블로킹하지 않음.
        Returns: request id
        """
        req_id = self._make_id()
        with self._lock:
            self._callbacks[req_id] = callback

        def _worker():
            result = self._send(
                {"action": "query", "question": question, "file": file},
                timeout=DEFAULT_TIMEOUT,
                req_id=req_id,
            )
            cb = self._callbacks.pop(req_id, None)
            if cb:
                cb(result)

        threading.Thread(target=_worker, daemon=True).start()
        return req_id

    def lint(self, timeout: float = LINT_TIMEOUT) -> dict:
        """wiki 건강 검진 (동기)."""
        return self._send({"action": "lint"}, timeout=timeout)

    # ──────────────────────────────────────────────
    # 내부 구현
    # ──────────────────────────────────────────────

    def _send(self, payload: dict, timeout: float = DEFAULT_TIMEOUT,
              req_id: str = None) -> dict:
        """요청 전송 후 응답 대기."""
        if not self._proc or self._proc.poll() is not None:
            if not self._try_restart():
                return {"status": "error", "message": "서버 프로세스 없음"}

        if req_id is None:
            req_id = self._make_id()
        payload["id"] = req_id

        event = threading.Event()
        with self._lock:
            self._pending[req_id] = event

        try:
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except Exception as e:
            with self._lock:
                self._pending.pop(req_id, None)
            return {"status": "error", "message": f"전송 실패: {e}"}

        if not event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(req_id, None)
                self._results.pop(req_id, None)
            return {"status": "error", "message": f"타임아웃 ({timeout}초)"}

        with self._lock:
            return self._results.pop(req_id, {"status": "error", "message": "응답 없음"})

    def _reader_loop(self) -> None:
        """stdout에서 JSON 응답을 읽어 pending 요청에 매칭."""
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    resp = json.loads(line)
                    req_id = resp.get("id")
                    if req_id:
                        with self._lock:
                            self._results[req_id] = resp
                            ev = self._pending.pop(req_id, None)
                        if ev:
                            ev.set()
                except json.JSONDecodeError:
                    logger.warning(f"응답 JSON 파싱 실패: {line[:100]}")
        except Exception as e:
            if self._running:
                logger.error(f"reader loop 오류: {e}")
        finally:
            # 진행 중인 요청 모두 에러 처리
            with self._lock:
                for req_id, ev in self._pending.items():
                    self._results[req_id] = {"status": "error",
                                             "message": "서버 연결 끊김"}
                    ev.set()
                self._pending.clear()

    def _stderr_loop(self) -> None:
        """stderr를 logging으로 전달."""
        try:
            for line in self._proc.stderr:
                line = line.rstrip()
                if line:
                    logger.debug(f"[wiki-server] {line}")
        except Exception:
            pass

    def _try_restart(self) -> bool:
        """프로세스 크래시 시 1회 재시작 시도."""
        logger.warning("Wiki server 재시작 시도")
        try:
            self.stop()
            self.start()
            return True
        except Exception as e:
            logger.error(f"재시작 실패: {e}")
            return False

    @staticmethod
    def _make_id() -> str:
        return str(uuid.uuid4())[:8]
