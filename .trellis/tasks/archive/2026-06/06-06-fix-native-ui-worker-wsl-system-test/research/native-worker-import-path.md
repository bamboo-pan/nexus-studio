# Native Worker Import Path

## Finding

The reported UI error is caused by the native UI worker child process running `python -m aistudio_api.infrastructure.gateway.native_ui_sender --worker` without an import path that contains the repo `src/` directory.

The parent service can still start through `python main.py` because the root wrapper mutates the parent process `sys.path`. That mutation is process-local; it is not inherited by subprocesses launched with `subprocess.Popen` unless the import root is exported through environment such as `PYTHONPATH` or the package is installed in the virtualenv.

## Repo Constraints

* `pyproject.toml` packages Python modules from `src/`.
* `main.py` supports source-tree execution by inserting `<repo>/src` into `sys.path`.
* Native worker children are intentionally separate clean processes and must keep using module execution for the worker entrypoint.
* System tests must not assume editable install or ambient `PYTHONPATH` is correct; they must explicitly prove the worker can import/start in the WSL temporary copy.

## Chosen Approach

Build the native worker subprocess environment inside `NativeUiWorker` by prepending the package import root derived from `native_ui_worker_pool.py` to `PYTHONPATH`. This preserves the current `python -m aistudio_api.infrastructure.gateway.native_ui_sender --worker` command while making source-tree parent execution and installed-package execution both deterministic.

System tests should include a hard worker import/start preflight and then perform UI smoke from the Windows host browser against the WSL service. `/api/local-studio/health` alone is not a worker availability oracle.