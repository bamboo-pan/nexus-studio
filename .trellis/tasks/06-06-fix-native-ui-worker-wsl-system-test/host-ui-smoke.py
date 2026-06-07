from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    port = os.environ["AISTUDIO_PORT"]
    model = os.environ["SYSTEM_TEST_MODEL"]
    artifact_dir = Path(os.environ["HOST_ARTIFACT_DIR"])
    artifact_dir.mkdir(parents=True, exist_ok=True)
    base_url = f"http://127.0.0.1:{port}"
    console_messages: list[str] = []
    network_events: list[dict[str, object]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on(
            "console",
            lambda message: console_messages.append(f"{message.type}:{message.text}") if message.type == "error" else None,
        )
        page.on(
            "response",
            lambda response: network_events.append({"url": response.url, "status": response.status})
            if "/api/local-studio/" in response.url
            else None,
        )
        page.goto(f"{base_url}/static/index.html#studio", wait_until="networkidle", timeout=60_000)
        page.wait_for_selector("#studio-page.active", timeout=30_000)
        page.evaluate(
            """(model) => {
                localStorage.setItem('openai.localStudio.settings.v1', JSON.stringify({
                    providers: [{
                        id: 'google-ai-studio',
                        type: 'google-ai-studio',
                        providerType: 'google-ai-studio',
                        name: 'Google AI Studio',
                        baseUrl: '',
                        apiKey: '',
                        timeout: 300,
                        interfaceMode: 'responses'
                    }],
                    providerId: 'google-ai-studio',
                    providerType: 'google-ai-studio',
                    name: 'Google AI Studio',
                    baseUrl: '',
                    apiKey: '',
                    timeout: 300,
                    interfaceMode: 'responses',
                    model,
                    imageModel: '',
                    stream: 'on',
                    reasoningEffort: 'off',
                    reasoningSummary: 'auto',
                    search: 'off',
                    imageToolEnabled: false,
                    imageSize: '1024x1024',
                    imageCustomSize: '',
                    imageQuality: 'auto',
                    imageBackground: 'auto',
                    imageFormat: 'png',
                    imageCompression: 100
                }));
            }""",
            model,
        )
        page.reload(wait_until="networkidle", timeout=60_000)
        page.wait_for_selector("#studio-page.active", timeout=30_000)
        page.wait_for_selector("#studio-page textarea[placeholder*='Local Studio']", timeout=30_000)
        page.evaluate(
            """(model) => {
                const root = Alpine.$data(document.body);
                root.localStudioProviderId = 'google-ai-studio';
                root.localStudioProviderType = 'google-ai-studio';
                root.localStudioSettings = {name: 'Google AI Studio', baseUrl: '', apiKey: '', timeout: 300};
                root.localStudioInterfaceMode = 'responses';
                root.localStudioModels = [{id: model, object: 'model', capabilities: {streaming: true, thinking: false, file_input: false}}];
                root.localStudioModel = model;
                root.localStudioStream = 'on';
                root.localStudioReasoningEffort = 'off';
                root.localStudioSearch = 'off';
                root.localStudioImageToolEnabled = false;
                root.saveLocalStudioSettings();
            }""",
            model,
        )
        page.locator("#studio-page textarea[placeholder*='Local Studio']").fill("Reply with exactly: nexus-native-worker-ui-ok")
        with page.expect_response(lambda response: "/api/local-studio/chat" in response.url, timeout=360_000) as response_info:
            page.locator("#studio-page .local-studio-compose-row button.send").click()
        chat_response = response_info.value
        page.wait_for_function(
            """() => {
                const root = Alpine.$data(document.body);
                return !root.localStudioBusy && root.localStudioActiveMessages.length >= 2;
            }""",
            timeout=360_000,
        )
        state = page.evaluate(
            """() => {
                const root = Alpine.$data(document.body);
                return {
                    view: root.view,
                    model: root.localStudioModel,
                    busy: root.localStudioBusy,
                    error: String(root.localStudioError || '').slice(0, 500),
                    messages: root.localStudioActiveMessages.map((message) => ({
                        role: message.role,
                        content: String(message.content || '').slice(0, 500),
                        error: String(message.error || '').slice(0, 500)
                    }))
                };
            }"""
        )
        page.screenshot(path=str(artifact_dir / "host-ui-local-studio.png"), full_page=True)
        page.goto(f"{base_url}/static/index.html#requests", wait_until="networkidle", timeout=60_000)
        page.wait_for_selector("#requests-page.active, .request-page.active", timeout=30_000)
        page.screenshot(path=str(artifact_dir / "host-ui-requests.png"), full_page=True)
        page.goto(f"{base_url}/static/index.html#accounts", wait_until="networkidle", timeout=60_000)
        page.wait_for_selector("#accounts-page.active, .accounts-page.active, .account-table-panel", timeout=30_000)
        page.screenshot(path=str(artifact_dir / "host-ui-accounts.png"), full_page=True)
        browser.close()

    result = {
        "chat_response_status": chat_response.status,
        "state": state,
        "console_errors": console_messages,
        "network_events": network_events,
        "screenshots": ["host-ui-local-studio.png", "host-ui-requests.png", "host-ui-accounts.png"],
    }
    (artifact_dir / "host-ui-result.safe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    assistant_messages = [message for message in state.get("messages", []) if message.get("role") == "assistant"]
    failures: list[str] = []
    if chat_response.status != 200:
        failures.append(f"chat_response_status={chat_response.status}")
    if console_messages:
        failures.append("console_errors")
    if state.get("error"):
        failures.append(f"local_studio_error={state['error']}")
    if not assistant_messages:
        failures.append("assistant_message_missing")
    if any(message.get("error") for message in assistant_messages):
        failures.append("assistant_message_error")
    if failures:
        print("HOST_UI_SMOKE_FAIL " + " ".join(failures))
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print("HOST_UI_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())