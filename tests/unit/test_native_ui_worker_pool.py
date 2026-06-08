import base64
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aistudio_api.infrastructure.gateway import native_ui_worker_pool as worker_pool_module
from aistudio_api.infrastructure.gateway.native_ui_worker_pool import (
    NativeUiWorker,
    NativeUiWorkerPool,
    NativeUiWorkerProcessError,
    NativeUiWorkerRequestError,
)


def _result(body: bytes, *, status: int = 200) -> dict[str, object]:
    return {
        "ok": True,
        "status": status,
        "body_b64": base64.b64encode(body).decode("ascii"),
        "wire_model": "models/gemini-3.5-flash",
        "body_size": len(body),
        "url_path": "/u/0/prompts/new_chat",
    }


def test_native_ui_worker_subprocess_env_prepends_package_import_root():
    worker = NativeUiWorker(index=0, env={"PYTHONPATH": "/existing/path", "AISTUDIO_CAMOUFOX_HEADLESS": "1"})

    expected_import_root = str(Path(worker_pool_module.__file__).resolve().parents[3])
    pythonpath_entries = worker._env["PYTHONPATH"].split(os.pathsep)

    assert pythonpath_entries[:2] == [expected_import_root, "/existing/path"]
    assert worker._env["AISTUDIO_CAMOUFOX_HEADLESS"] == "1"


def test_native_ui_worker_pool_reuses_single_worker():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = []
            self.closed = False
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls.append(dict(payload))
            return _result(f"ok-{len(self.calls)}".encode("utf-8"))

        def restart(self):
            raise AssertionError("restart should not be needed")

        def close(self):
            self.closed = True

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=1, worker_factory=FakeWorker)

    status1, raw1 = pool.send(model="models/gemini-3.5-flash", prompt="one", timeout_ms=1000)
    status2, raw2 = pool.send(model="models/gemini-3.5-flash", prompt="two", timeout_ms=1000)

    assert (status1, raw1) == (200, b"ok-1")
    assert (status2, raw2) == (200, b"ok-2")
    assert len(workers) == 1
    assert [call["prompt"] for call in workers[0].calls] == ["one", "two"]
    pool.close()
    assert workers[0].closed is True


def test_native_ui_worker_pool_can_return_response_metadata():
    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index

        def send(self, payload, *, timeout_seconds):
            return _result(b"metadata-ok", status=201)

        def restart(self):
            raise AssertionError("restart should not be needed")

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=1, worker_factory=FakeWorker)

    status, raw, metadata = pool.send_with_metadata(model="models/gemini-3.5-flash", prompt="warmup", timeout_ms=1000)

    assert (status, raw) == (201, b"metadata-ok")
    assert metadata["wire_model"] == "models/gemini-3.5-flash"
    assert metadata["body_size"] == len(b"metadata-ok")


def test_native_ui_worker_pool_prefers_recent_success_for_serial_requests():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            return _result(f"worker-{self.index}-call-{self.calls}".encode("utf-8"))

        def restart(self):
            raise AssertionError("restart should not be needed")

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=3, worker_factory=FakeWorker)

    results = [pool.send(model="models/gemini-3.5-flash", prompt=f"prompt-{index}", timeout_ms=1000) for index in range(3)]

    assert [raw for _status, raw in results] == [b"worker-2-call-1", b"worker-2-call-2", b"worker-2-call-3"]
    assert [worker.calls for worker in workers] == [0, 0, 3]


def test_native_ui_worker_pool_can_round_robin_when_recent_preference_disabled():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            return _result(f"worker-{self.index}".encode("utf-8"))

        def restart(self):
            raise AssertionError("restart should not be needed")

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=3, worker_factory=FakeWorker)

    results = [
        pool.send_with_metadata(
            model="models/gemini-3.5-flash",
            prompt=f"probe-{index}",
            timeout_ms=1000,
            max_attempts=1,
            prefer_recent_worker=False,
        )
        for index in range(3)
    ]

    assert [raw for _status, raw, _metadata in results] == [b"worker-0", b"worker-1", b"worker-2"]
    assert [worker.calls for worker in workers] == [1, 1, 1]


def test_native_ui_worker_pool_restarts_and_retries_single_worker_failure():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            self.restarts = 0
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            if self.calls == 1:
                raise NativeUiWorkerProcessError("process exited")
            return _result(b"after-restart")

        def restart(self):
            self.restarts += 1

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=1, worker_factory=FakeWorker)

    status, raw = pool.send(model="models/gemini-3.5-flash", prompt="retry", timeout_ms=1000, prefer_recent_worker=False)

    assert (status, raw) == (200, b"after-restart")
    assert workers[0].calls == 2
    assert workers[0].restarts == 1


def test_native_ui_worker_pool_retries_request_failure_without_restart():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            self.restarts = 0
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            if self.calls == 1:
                return {"ok": False, "error": "AI Studio text model not selected"}
            return _result(b"after-request-retry")

        def restart(self):
            self.restarts += 1

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=1, worker_factory=FakeWorker)

    status, raw = pool.send(model="models/gemini-3.5-flash", prompt="retry", timeout_ms=1000)

    assert (status, raw) == (200, b"after-request-retry")
    assert workers[0].calls == 2
    assert workers[0].restarts == 0


def test_native_ui_worker_pool_restarts_after_navigation_request_failure():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            self.restarts = 0
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            if self.calls == 1:
                return {"ok": False, "error": "AI Studio chat runtime not ready in native UI sender after 180000ms"}
            return _result(b"after-navigation-restart")

        def restart(self):
            self.restarts += 1

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=1, worker_factory=FakeWorker)

    status, raw = pool.send(model="models/gemini-3.5-flash", prompt="retry", timeout_ms=1000)

    assert (status, raw) == (200, b"after-navigation-restart")
    assert workers[0].calls == 2
    assert workers[0].restarts == 1


def test_native_ui_worker_pool_restarts_after_current_model_readback_failure():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            self.restarts = 0
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            if self.calls == 1:
                return {
                    "ok": False,
                    "error": (
                        "RuntimeError: AI Studio text model not selected in native UI sender: gemini-3.5-flash; "
                        "current={'matches': False, 'reason': 'current_text_model_not_found'} "
                        "opened={'opened': True, 'type': 'text_category'}"
                    ),
                }
            return _result(b"after-model-readback-restart")

        def restart(self):
            self.restarts += 1

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=1, worker_factory=FakeWorker)

    status, raw = pool.send(model="models/gemini-3.5-flash", prompt="retry", timeout_ms=1000)

    assert (status, raw) == (200, b"after-model-readback-restart")
    assert workers[0].calls == 2
    assert workers[0].restarts == 1


def test_native_ui_worker_pool_restarts_after_target_closed_request_failure():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            self.restarts = 0
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            if self.calls == 1:
                return {"ok": False, "error": "TargetClosedError: Page.wait_for_timeout: Target page, context or browser has been closed"}
            return _result(b"after-target-closed-restart")

        def restart(self):
            self.restarts += 1

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=1, worker_factory=FakeWorker)

    status, raw = pool.send(model="models/gemini-3.5-flash", prompt="retry", timeout_ms=1000)

    assert (status, raw) == (200, b"after-target-closed-restart")
    assert workers[0].calls == 2
    assert workers[0].restarts == 1


def test_native_ui_worker_pool_can_limit_attempts_for_startup_probe():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            self.restarts = 0
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            return {"ok": False, "error": f"AI Studio chat runtime not ready on worker-{self.index}"}

        def restart(self):
            self.restarts += 1

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=3, worker_factory=FakeWorker)

    with pytest.raises(NativeUiWorkerRequestError, match="worker-0"):
        pool.send_with_metadata(
            model="models/gemini-3.5-flash",
            prompt="warmup",
            timeout_ms=1000,
            max_attempts=1,
            prefer_recent_worker=False,
        )

    assert [worker.calls for worker in workers] == [1, 0, 0]
    assert [worker.restarts for worker in workers] == [1, 0, 0]


def test_native_ui_worker_pool_retries_request_failure_across_configured_workers():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            self.restarts = 0
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            if self.index < 2:
                return {"ok": False, "error": f"worker-{self.index} picker cold"}
            return _result(b"third-worker-ok")

        def restart(self):
            self.restarts += 1

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=3, worker_factory=FakeWorker)

    status, raw = pool.send(model="models/gemini-3.5-flash", prompt="retry", timeout_ms=1000, prefer_recent_worker=False)

    assert (status, raw) == (200, b"third-worker-ok")
    assert [worker.calls for worker in workers] == [1, 1, 1]
    assert [worker.restarts for worker in workers] == [0, 0, 0]


def test_native_ui_worker_pool_retries_configured_status_across_workers():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            self.restarts = 0
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            if self.index == 0:
                return _result(b"permission denied", status=403)
            return _result(b"second-worker-ok")

        def restart(self):
            self.restarts += 1

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=2, worker_factory=FakeWorker)

    status, raw = pool.send(
        model="models/gemini-3.5-flash",
        prompt="retry",
        timeout_ms=1000,
        retry_statuses=(401, 403),
        prefer_recent_worker=False,
    )

    assert (status, raw) == (200, b"second-worker-ok")
    assert [worker.calls for worker in workers] == [2, 1]
    assert [worker.restarts for worker in workers] == [0, 0]


def test_native_ui_worker_pool_retries_configured_status_on_same_worker_first():
    workers = []

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            self.restarts = 0
            workers.append(self)

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            if self.index == 0 and self.calls == 1:
                return _result(b"first permission denied", status=403)
            if self.index == 0:
                return _result(b"same-worker-ok")
            raise AssertionError("next worker should not be leased after same-context recovery")

        def restart(self):
            self.restarts += 1

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=2, worker_factory=FakeWorker)

    status, raw = pool.send(
        model="models/gemini-3.5-flash",
        prompt="retry",
        timeout_ms=1000,
        retry_statuses=(401, 403),
        prefer_recent_worker=False,
    )

    assert (status, raw) == (200, b"same-worker-ok")
    assert [worker.calls for worker in workers] == [2, 0]
    assert [worker.restarts for worker in workers] == [0, 0]


def test_native_ui_worker_pool_preserves_last_worker_error_when_retry_budget_expires(monkeypatch):
    monotonic_values = iter([1000.0, 1001.0, 1002.0, 2000.0])
    monkeypatch.setattr(worker_pool_module.time, "monotonic", lambda: next(monotonic_values, 2000.0))

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index
            self.calls = 0
            self.restarts = 0

        def send(self, payload, *, timeout_seconds):
            self.calls += 1
            raise NativeUiWorkerProcessError("real native sender navigation timeout; stderr=stage=open_chat.goto")

        def restart(self):
            self.restarts += 1

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=1, worker_factory=FakeWorker)

    with pytest.raises(NativeUiWorkerProcessError) as exc_info:
        pool.send(model="models/gemini-3.5-flash", prompt="retry", timeout_ms=1000)

    message = str(exc_info.value)
    assert "real native sender navigation timeout" in message
    assert "stage=open_chat.goto" in message
    assert "timed out waiting for an available worker" not in message


def test_native_ui_worker_pool_leases_multiple_workers_for_concurrent_requests():
    barrier = threading.Barrier(2)
    used_indexes = []
    lock = threading.Lock()

    class FakeWorker:
        def __init__(self, *, index, command=None, env=None):
            self.index = index

        def send(self, payload, *, timeout_seconds):
            with lock:
                used_indexes.append(self.index)
            barrier.wait(timeout=5)
            return _result(f"worker-{self.index}".encode("utf-8"))

        def restart(self):
            raise AssertionError("restart should not be needed")

        def close(self):
            pass

    pool = NativeUiWorkerPool(auth_file="/tmp/auth.json", worker_count=2, worker_factory=FakeWorker)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(pool.send, model="models/gemini-3.5-flash", prompt=f"prompt-{index}", timeout_ms=1000)
            for index in range(2)
        ]
        results = [future.result(timeout=5) for future in futures]

    assert sorted(used_indexes) == [0, 1]
    assert sorted(raw for _status, raw in results) == [b"worker-0", b"worker-1"]