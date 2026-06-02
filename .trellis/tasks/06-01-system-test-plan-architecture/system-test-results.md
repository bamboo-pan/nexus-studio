# System Test Results

## Local Verification

- `git diff --check`: passed.
- VS Code diagnostics for changed docs/tests: passed.
- `C:/Users/bamboo/Desktop/nexus-studio/.venv/Scripts/python.exe -m pytest tests/unit/test_system_test_plan_architecture_contract.py tests/unit`: 375 passed.

## WSL Real API/UI Smoke

- Run root: `/home/bamboo/nexus-studio-system-test-20260601-205000`.
- Artifact summary: `/home/bamboo/nexus-studio-system-test-20260601-205000/artifacts/summary.md`.
- API checks: 16/17 passed.
- UI checks: 6/6 passed.
- Passed paths included `/v1/models`, `/v1beta/models`, Local Studio Google/OpenAI model loading, OpenAI-compatible non-stream and stream chat, UI navigation for Studio/Chat/Images/Requests/Accounts, Local Studio send, secret scan, and Provider Manager phase-gate detection.

## Blocking Failure

The warmup-aware full system gate failed because global `/health` reported startup warmup as failed:

```json
{"status":"failed","target_accounts":["acc_180b3249"],"completed_accounts":[],"failed_accounts":["acc_180b3249"]}
```

Server log signature:

```text
Account browser warmup failed for account=acc_180b3249: Page.goto: Timeout 60000ms exceeded.
```

This blocks claiming a complete system-test pass under `SYSTEM_TEST_PLAN.md`, which requires warmup status to reach `complete` or be explicitly classified as a controlled limitation.