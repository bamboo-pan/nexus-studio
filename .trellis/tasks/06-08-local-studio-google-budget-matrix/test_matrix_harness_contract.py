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
    assert "complete ui-results.json P0/P1 matrix" not in runner
    assert "G-LS-02-through-G-LS-11-google-ui-matrix" not in runner
    assert "O-LS-02-through-O-LS-10-openai-ui-matrix" not in runner
    assert "LS-UI-remaining-matrix" not in runner
    assert "plan_script_alignment_failed" not in runner


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