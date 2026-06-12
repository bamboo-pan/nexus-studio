import importlib.util
from pathlib import Path


TASK_DIR = Path(__file__).resolve().parent


def test_system_test_runner_maps_required_rows_explicitly():
    runner = (TASK_DIR / "system-test-wsl.sh").read_text(encoding="utf-8")

    assert "required_case_ids = [" in runner
    assert "unmapped_required_cases" in runner
    assert "failing_required_cases" in runner
    assert "not_applicable_cases" in runner
    assert "matrix_mapping_complete" in runner
    assert "post_local_studio_with_retries" in runner
    assert "sentinel_matched" in runner
    assert "AI Studio native UI worker replay matched response:" in runner
    assert "google-quota-blocker.safe.json" in runner
    assert "external_google_quota_blocked" in runner
    assert "SYSTEM_TEST_BLOCKED external_google_quota_exhausted" in runner
    assert "startup-account-browser-warmup" in runner
    assert "write_warmup_quota_blocker" in runner
    assert "post_google_with_model_fallback" in runner
    assert "post_google_text_api_with_model_fallback" in runner
    assert "API_REAL_PROVIDER_BLOCKED external_google_quota_exhausted" in runner
    assert "HOST_UI_SMOKE_SKIP external_google_quota_exhausted" in runner
    assert "SYSTEM_TEST_MODEL_CANDIDATES_PS" in runner
    assert r"\$env:SYSTEM_TEST_MODEL_CANDIDATES" in runner
    assert "candidate_chain_exhausted" in runner
    assert "failed_candidate_reasons" in runner
    assert "quota_or_unavailable_scope" in runner
    assert "SYSTEM_TEST_RESUME_FROM_RUN_ROOT" in runner
    assert "SYSTEM_TEST_RESUME_OFFICIAL_BASELINE_FROM_RUN_ROOT" in runner
    assert "resume-evidence.safe.json" in runner
    assert "resume-current-run-setup.safe.json" in runner
    assert "API_REAL_PROVIDER_REUSED" in runner
    assert "architecture-contract-results.json" in runner
    assert "REQUEST_LOG_AND_ARCHITECTURE_ORACLE_REUSED" in runner
    assert "current_run_request_log_oracle_skipped" in runner
    assert "resume_api_artifact_incomplete" in runner
    assert '"checkpoint": "api_complete"' in runner
    assert "REQUEST_LOG_AND_ARCHITECTURE_ORACLE_SKIP external_google_quota_exhausted" in runner
    assert "exit \\$LASTEXITCODE" in runner
    assert "complete ui-results.json P0/P1 matrix" not in runner
    assert "G-LS-02-through-G-LS-11-google-ui-matrix" not in runner
    assert "O-LS-02-through-O-LS-10-openai-ui-matrix" not in runner
    assert "LS-UI-remaining-matrix" not in runner
    assert "plan_script_alignment_failed" not in runner
    assert '"interface_mode": "chat_completions"' not in runner
    assert "cache_attempt_items" in runner
    assert "attempt_result_count" in runner
    assert "base_api_text_from_data" in runner
    assert "candidate_sentinel_mismatch" in runner
    assert "candidate_no_available_account" in runner
    assert "AISTUDIO_ACCOUNT_QUOTA_EXHAUSTED_COOLDOWN_SECONDS" in runner
    assert "AISTUDIO_PROXY_SERVER_BRIDGED" in runner
    assert "AISTUDIO_PROXY_SERVER_PRESET" in runner
    assert "export AISTUDIO_PROXY_SERVER" in runner
    assert "sentinel_matched" in runner
    assert "expected_text=\"nexus-base-responses-ok\"" in runner


def test_system_test_runner_includes_current_plan_rows():
    runner = (TASK_DIR / "system-test-wsl.sh").read_text(encoding="utf-8")

    required_rows = [
        "ENV-01",
        "BOOT-01",
        "PM-ROLL-00",
        "PM-CP-01",
        "PM-DP-01",
        "G-LS-01",
        "G-LS-11",
        "O-LS-01",
        "O-LS-10",
        "LS-UI-15",
        "BASE-ACC-02",
        "API-LS-10",
        "BUG-AISTUDIO-NATIVE-MODEL-SELECTION-01",
        "PERF-01",
    ]
    for case_id in required_rows:
        assert f'"{case_id}"' in runner


def test_host_ui_quota_blocker_classifier_is_specific():
    module_path = TASK_DIR / "host-ui-smoke.py"
    spec = importlib.util.spec_from_file_location("host_ui_smoke_for_contract", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.google_quota_exhausted_text(
        "You exceeded your current quota, please check your plan. https://ai.google.dev/gemini-api/docs/rate-limits"
    )
    assert module.google_quota_exhausted_text("RESOURCE_EXHAUSTED: quota resets tomorrow")
    assert not module.google_quota_exhausted_text("quota exhausted")
    assert not module.google_quota_exhausted_text("ordinary 429 rate limited")


def test_host_ui_performance_samples_are_isolated_and_timeout_retryable():
    module_path = TASK_DIR / "host-ui-smoke.py"
    source = module_path.read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location("host_ui_smoke_for_performance_contract", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert "create_local_studio_conversation_via_ui(page, actions, f\"google-ai-studio-performance" in source
    assert "send_local_studio_message_with_transient_retry(" in source
    assert "new_conversation_each_attempt=True" in source
    assert "system_test_google_model_candidates" in source
    assert "dedupe_model_candidates" in source
    assert "click_select_option_if_present(page, \"Conversation Model\", candidate_model)" in source
    assert "google performance model candidate quota-limited; trying next candidate" in source
    assert "select_playground_model_if_present" in source
    assert "playground base chat model candidate quota-limited; trying next candidate" in source
    assert "playground search model candidate quota-limited; trying next candidate" in source
    assert "playground_model_candidates = dedupe_model_candidates([google_selected_model, *google_model_candidates])" in source
    assert "model_candidates_exhausted" in source
    assert "local_google_performance_candidate_results" in source
    assert module.dedupe_model_candidates(["gemini-a", " gemini-a ", "gemini-b", ""]) == ["gemini-a", "gemini-b"]
    assert module.split_model_candidates("gemini-a; gemini-b\ngemini-c, gemini-d") == [
        "gemini-a",
        "gemini-b",
        "gemini-c",
        "gemini-d",
    ]
    assert module.transient_upstream_error_text("APIRequestContext.post: Timeout 120000ms exceeded.")
    assert module.transient_upstream_error_text("HTTP 502: AI Studio returned no response content")
    assert module.transient_upstream_error_text("HTTP 0: APIRequestContext.post: connect ENETUNREACH 2404:6800:4005:81f::200a:443")
    assert "base #images edit retry after transient upstream error" in source
    assert "edit_retry_attempts" in source
    assert "last_edit_error_prefix" in source
    assert "base #images edit/retry failed after" in source
    assert "base #images edit fallback image model" in source
    assert "edit_model_attempts" in source
    assert "recover_aligned_account_after_unavailable" in source
    assert "no available account" in source
    assert "f\"{provider_kind}-{key}-repeat\"" in source
    assert "page.locator(\".image-page.active .image-error\").get_by_role(\"button\", name=\"重试\")" in source


def test_host_ui_can_reuse_valid_official_baseline(tmp_path, monkeypatch):
        module_path = TASK_DIR / "host-ui-smoke.py"
        spec = importlib.util.spec_from_file_location("host_ui_smoke_for_resume_contract", module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        source = tmp_path / "official.json"
        source.write_text(
                """
{
    "result": "pass",
    "samples": [{"status": 200}, {"status": 200}, {"status": 200}],
    "account_id": "acc_resume"
}
""".strip(),
                encoding="utf-8",
        )
        target_dir = tmp_path / "artifacts"
        target_dir.mkdir()
        monkeypatch.setenv("HOST_OFFICIAL_BASELINE_RESULTS_FILE", str(source))
        actions: list[str] = []

        result = module.load_reused_official_baseline(target_dir, actions)

        assert result["result"] == "pass"
        assert result["reuse_mode"] == "low_quota_resume"
        assert result["reused_from_artifact"] == str(source)
        assert (target_dir / "host-official-aistudio-results.json").is_file()
        assert any("reuse official AI Studio visible baseline" in action for action in actions)