# Fix Native UI Worker WSL System Test

## Goal

Fix the native UI worker startup failure reported from real UI usage and update the system test plan so WSL service startup plus host-browser Playwright UI testing cannot assume worker availability.

## What I Already Know

* User hit `Error 502: HTTP 503: native UI worker unavailable: native UI worker pool failed after restart: native UI worker 2 exited with code 1`.
* The worker stderr was `/home/bamboo/nexus-studio/.venv/bin/python3: Error while finding module specification for 'aistudio_api.infrastructure.gateway.native_ui_sender' (ModuleNotFoundError: No module named 'aistudio_api')`.
* The server entrypoint `main.py` inserts repo `src/` into the parent process `sys.path`, but child worker processes are started with `python -m aistudio_api.infrastructure.gateway.native_ui_sender --worker` and do not automatically inherit that `sys.path`.
* Real system tests must run in a WSL temporary repo copy, start the service in WSL, then exercise the UI from the Windows host browser with Playwright.

## Requirements

* Native UI worker subprocesses must be able to import `aistudio_api.infrastructure.gateway.native_ui_sender` when the parent service is started from the source-tree wrapper `python main.py`, not only when editable install/PYTHONPATH happens to be present.
* The failure must surface as a controlled native worker failure if the worker cannot start, but the normal supported path should not fail due to missing package import path.
* Add unit coverage for the worker subprocess environment/import path contract.
* Update `SYSTEM_TEST_PLAN.md` to require explicit WSL worker import/startup preflight and host-browser Playwright UI validation before declaring system tests passed.
* Run focused unit tests plus real WSL API and host-browser UI smoke for the native worker/Local Studio path.

## Acceptance Criteria

* [ ] Unit tests cover native UI worker environment setup.
* [ ] WSL smoke starts the service from a temporary repo copy and proves the worker module can import/start before UI assertions.
* [ ] Host-browser Playwright UI smoke reaches the Local Studio UI and exercises the reported path far enough to avoid the missing-module worker failure.
* [ ] `SYSTEM_TEST_PLAN.md` forbids treating API startup or `/api/local-studio/health` alone as proof that native UI workers are available.
* [ ] No real credentials, cookies, storage state, tokens, screenshots, or request logs are committed.

## Out of Scope

* Broad Local Studio feature redesign.
* Changing native UI model selection behavior unless needed to reach this import/startup fix.
* Replacing the full system-test matrix with a new harness in this task.

## Technical Notes

* Primary code path: `src/aistudio_api/infrastructure/gateway/native_ui_worker_pool.py`.
* Worker module: `src/aistudio_api/infrastructure/gateway/native_ui_sender.py`.
* Existing native worker scenario spec: `.trellis/spec/backend/quality-guidelines.md`.
* Research note: `research/native-worker-import-path.md`.