from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read_doc(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def assert_contains_all(document: str, required_terms: list[str]):
    missing_terms = [term for term in required_terms if term not in document]
    assert missing_terms == []


def test_system_test_plan_tracks_provider_manager_pool_architecture():
    architecture = read_doc("ARCHITECTURE.md")
    plan = read_doc("SYSTEM_TEST_PLAN.md")

    assert_contains_all(
        architecture,
        [
            "Provider Manager",
            "provider-model",
            "控制面",
            "数据面",
            "OpenAI Responses",
            "OpenAI Chat Completions",
            "Gemini",
            "Claude Messages",
            "路由策略",
        ],
    )

    assert_contains_all(
        plan,
        [
            "Provider Manager / shared provider-model pool",
            "Provider Manager control plane",
            "shared runtime gateway data plane",
            "provider/model registry",
            "credential references",
            "model catalog",
            "health checks",
            "routing policies",
            "audit safety",
            "canonical request",
            "canonical response",
            "protocol adapters",
            "provider executors",
            "response conversion",
            "OpenAI Responses",
            "OpenAI Chat Completions",
            "Gemini",
            "Claude Messages",
            "aliases/defaults",
            "capability matching",
            "priority/weight",
            "fallback",
            "sticky routing",
            "streaming/tool/image compatibility",
            "Rollout phase gates",
        ],
    )


def test_system_test_plan_has_executable_provider_manager_phase_gates():
    plan = read_doc("SYSTEM_TEST_PLAN.md")

    assert_contains_all(
        plan,
        [
            "| PM-ROLL-00 | Phase 0+ |",
            "| PM-CP-01 | Phase 1+ |",
            "| PM-CP-02 | Phase 1+ |",
            "| PM-CP-03 | Phase 1+ |",
            "| PM-AUDIT-01 | Phase 1+ |",
            "| PM-DP-01 | Phase 2+ |",
            "| PM-PROTO-01 | Phase 2+ |",
            "| PM-RT-01 | Phase 2+ |",
            "| PM-RT-02 | Phase 3+ |",
        ],
    )

    assert_contains_all(
        plan,
        [
            "architecture-contract-results",
            "provider-manager-phase-gate-results",
            "pass/fail/not_applicable",
            "request log group",
            "routing decision",
            "attempt plan",
        ],
    )


def test_system_test_plan_gates_future_runtime_claims_with_evidence():
    plan = read_doc("SYSTEM_TEST_PLAN.md")

    assert_contains_all(
        plan,
        [
            "某阶段尚未实现时，可以标记 `not_applicable`",
            "未实现阶段必须有代码证据",
            "尚未实现 Provider Manager route/page/registry/gateway",
            "不得把目标架构缺失误报为已经通过",
            "当前 rollout phase 尚未进入该能力",
            "一旦某个阶段的任一用户入口、API route、存储 schema 或 routing function 出现，对应阶段不能整体标记不适用",
        ],
    )