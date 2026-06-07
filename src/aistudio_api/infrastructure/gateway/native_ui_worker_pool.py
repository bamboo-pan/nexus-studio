"""Persistent native AI Studio UI worker pool.

The parent process owns a small pool per account. Each child process runs the
clean native UI sender in JSONL worker mode, keeping its own Camoufox process
outside the gateway hook/template browser process.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger("aistudio.native_worker_pool")


class NativeUiWorkerError(RuntimeError):
    """Base error for native UI worker pool failures."""


class NativeUiWorkerProcessError(NativeUiWorkerError):
    """The worker process exited or could not accept a request."""


class NativeUiWorkerProtocolError(NativeUiWorkerError):
    """The worker process violated the JSONL protocol."""


class NativeUiWorkerTimeoutError(NativeUiWorkerError, TimeoutError):
    """The worker did not answer within the request budget."""


class NativeUiWorkerRequestError(NativeUiWorkerError):
    """The worker stayed alive but could not complete this UI request."""


def _default_worker_command() -> list[str]:
    return [sys.executable, "-m", "aistudio_api.infrastructure.gateway.native_ui_sender", "--worker"]


def _package_import_root() -> str:
    return str(Path(__file__).resolve().parents[3])


def _worker_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    worker_env = dict(env or os.environ.copy())
    import_root = _package_import_root()
    pythonpath = worker_env.get("PYTHONPATH") or ""
    entries = [entry for entry in pythonpath.split(os.pathsep) if entry]
    if import_root not in entries:
        entries.insert(0, import_root)
    worker_env["PYTHONPATH"] = os.pathsep.join(entries)
    return worker_env


def _safe_text(value: object, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _request_error_requires_restart(exc: BaseException) -> bool:
    message = str(exc).lower()
    restart_markers = (
        "ai studio chat runtime not ready",
        "ai studio image runtime not ready",
        "native ui sender timeout",
        "page.goto: timeout",
        "navigation timeout",
    )
    return any(marker in message for marker in restart_markers)


class NativeUiWorker:
    """One long-lived native UI sender subprocess."""

    def __init__(
        self,
        *,
        index: int,
        command: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.index = index
        self._command = list(command or _default_worker_command())
        self._env = _worker_environment(env)
        self._process: subprocess.Popen[str] | None = None
        self._stdout_queue: queue.Queue[str] = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=40)
        self._io_lock = threading.Lock()

    def send(self, payload: dict[str, object], *, timeout_seconds: float) -> dict[str, object]:
        with self._io_lock:
            process = self._ensure_started()
            request_id = uuid.uuid4().hex
            message = json.dumps({"id": request_id, "payload": payload}, ensure_ascii=False)
            try:
                assert process.stdin is not None
                process.stdin.write(message + "\n")
                process.stdin.flush()
            except Exception as exc:
                self.close()
                raise NativeUiWorkerProcessError(f"native UI worker {self.index} write failed: {_safe_text(exc)}") from exc

            deadline = time.monotonic() + max(1.0, timeout_seconds)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stderr = self._stderr_summary()
                    self.close()
                    detail = f"; stderr={stderr}" if stderr else ""
                    raise NativeUiWorkerTimeoutError(f"native UI worker {self.index} timed out{detail}")
                if process.poll() is not None:
                    stderr = self._stderr_summary()
                    self.close()
                    raise NativeUiWorkerProcessError(
                        f"native UI worker {self.index} exited with code {process.returncode}; stderr={stderr}"
                    )
                try:
                    line = self._stdout_queue.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                parsed = self._parse_stdout_line(line)
                if parsed is None:
                    continue
                if parsed.get("id") != request_id:
                    log.debug("Ignoring native UI worker %s message for id=%s", self.index, parsed.get("id"))
                    continue
                if parsed.get("ok") is False and not parsed.get("fatal"):
                    stderr = self._stderr_summary()
                    if stderr:
                        parsed["stderr"] = stderr
                return parsed

    def restart(self) -> None:
        self.close()

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass

    def _ensure_started(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self._drain_stdout_queue()
        self._stderr_lines.clear()
        process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=self._env,
        )
        self._process = process
        threading.Thread(target=self._read_stdout, args=(process,), name=f"native-ui-worker-{self.index}-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(process,), name=f"native-ui-worker-{self.index}-stderr", daemon=True).start()
        log.info("Started native UI worker index=%s pid=%s", self.index, process.pid)
        return process

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is None:
            return
        for line in stream:
            self._stdout_queue.put(line)

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        stream = process.stderr
        if stream is None:
            return
        for line in stream:
            text = line.strip()
            if text:
                self._stderr_lines.append(text)

    def _parse_stdout_line(self, line: str) -> dict[str, object] | None:
        candidate = line.strip()
        if not candidate.startswith("{"):
            return None
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            raise NativeUiWorkerProtocolError(f"native UI worker {self.index} produced malformed JSON: {_safe_text(candidate)}")
        if not isinstance(parsed, dict):
            raise NativeUiWorkerProtocolError(f"native UI worker {self.index} produced non-object JSON: {_safe_text(candidate)}")
        return parsed

    def _stderr_summary(self) -> str:
        return _safe_text(" | ".join(self._stderr_lines), 500)

    def _drain_stdout_queue(self) -> None:
        while True:
            try:
                self._stdout_queue.get_nowait()
            except queue.Empty:
                return


WorkerFactory = Callable[..., Any]


class NativeUiWorkerPool:
    """Fixed-size pool of native UI sender subprocesses for one auth file."""

    def __init__(
        self,
        *,
        auth_file: str,
        worker_count: int,
        worker_factory: WorkerFactory = NativeUiWorker,
        command: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be >= 1")
        self.auth_file = auth_file
        self.worker_count = worker_count
        self.auth_hash = hashlib.sha256(auth_file.encode("utf-8", errors="replace")).hexdigest()[:12]
        self._closed = False
        self._workers = [worker_factory(index=index, command=command, env=env) for index in range(worker_count)]
        self._available: queue.Queue[Any] = queue.Queue(maxsize=worker_count)
        for worker in self._workers:
            self._available.put(worker)
        log.info("AI Studio native UI worker pool ready: auth_hash=%s worker_count=%s", self.auth_hash, worker_count)

    def send(self, *, model: str, prompt: str, timeout_ms: int, max_attempts: int | None = None) -> tuple[int, bytes]:
        status, raw, _metadata = self.send_with_metadata(model=model, prompt=prompt, timeout_ms=timeout_ms, max_attempts=max_attempts)
        return status, raw

    def send_with_metadata(
        self,
        *,
        model: str,
        prompt: str,
        timeout_ms: int,
        max_attempts: int | None = None,
    ) -> tuple[int, bytes, dict[str, object]]:
        if self._closed:
            raise NativeUiWorkerProcessError("native UI worker pool is closed")
        payload = {
            "auth_file": self.auth_file,
            "model": model,
            "prompt": prompt,
            "timeout_ms": timeout_ms,
        }
        timeout_seconds = max(60.0, float(timeout_ms) / 1000.0 + 90.0)
        deadline = time.monotonic() + timeout_seconds
        last_error: BaseException | None = None
        attempts = max(1, int(max_attempts)) if max_attempts is not None else max(2, self.worker_count)
        for _ in range(attempts):
            try:
                worker = self._lease_worker(deadline)
            except NativeUiWorkerTimeoutError as exc:
                if last_error is None:
                    last_error = exc
                else:
                    log.warning("Native UI worker pool budget exhausted while waiting for retry worker; last_error=%s", last_error)
                break
            try:
                result = worker.send(payload, timeout_seconds=max(1.0, deadline - time.monotonic()))
                status, raw = self._decode_result(result)
                log.info(
                    "AI Studio native UI worker replay matched response: auth_hash=%s worker=%s status=%s wire_model=%s body_size=%s url_path=%s",
                    self.auth_hash,
                    getattr(worker, "index", "?"),
                    status,
                    result.get("wire_model") or "",
                    result.get("body_size") or len(raw),
                    result.get("url_path") or "",
                )
                return status, raw, dict(result)
            except NativeUiWorkerRequestError as exc:
                last_error = exc
                if _request_error_requires_restart(exc):
                    try:
                        worker.restart()
                    except Exception:
                        pass
                    log.warning("Restarted native UI worker after request failure: %s", exc)
                else:
                    log.warning("Native UI worker request failed without process restart: %s", exc)
            except (NativeUiWorkerProcessError, NativeUiWorkerProtocolError, NativeUiWorkerTimeoutError) as exc:
                last_error = exc
                try:
                    worker.restart()
                except Exception:
                    pass
                log.warning("Restarted native UI worker after protocol/process failure: %s", exc)
            finally:
                self._release_worker(worker)
        if last_error is not None:
            if isinstance(last_error, NativeUiWorkerRequestError):
                raise NativeUiWorkerRequestError(f"native UI worker pool request failed after retry: {last_error}") from last_error
            raise NativeUiWorkerProcessError(f"native UI worker pool failed after restart: {last_error}") from last_error
        raise NativeUiWorkerProcessError("native UI worker pool failed without an available worker")

    def close(self) -> None:
        self._closed = True
        for worker in self._workers:
            try:
                worker.close()
            except Exception:
                pass

    def _lease_worker(self, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NativeUiWorkerTimeoutError("native UI worker pool timed out waiting for an available worker")
        try:
            return self._available.get(timeout=remaining)
        except queue.Empty as exc:
            raise NativeUiWorkerTimeoutError("native UI worker pool timed out waiting for an available worker") from exc

    def _release_worker(self, worker: Any) -> None:
        if self._closed:
            return
        try:
            self._available.put_nowait(worker)
        except queue.Full:
            pass

    def _decode_result(self, result: dict[str, object]) -> tuple[int, bytes]:
        if result.get("fatal"):
            raise NativeUiWorkerProtocolError(str(result.get("error") or "native UI worker fatal error"))
        if not result.get("ok"):
            stderr = _safe_text(result.get("stderr"), 500)
            detail = f"; stderr={stderr}" if stderr else ""
            raise NativeUiWorkerRequestError(str(result.get("error") or "native UI worker request failed") + detail)
        try:
            status = int(result.get("status") or 0)
            raw = base64.b64decode(str(result.get("body_b64") or ""))
        except Exception as exc:
            raise NativeUiWorkerProtocolError(f"native UI worker returned malformed response: {result!r}") from exc
        return status, raw