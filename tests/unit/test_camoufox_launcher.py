import subprocess
import sys
from pathlib import Path

from aistudio_api.config import settings
from aistudio_api.infrastructure.browser import camoufox_launcher


def test_camoufox_launcher_file_execution_can_import_project_package():
    launcher = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "aistudio_api"
        / "infrastructure"
        / "browser"
        / "camoufox_launcher.py"
    )

    result = subprocess.run(
        [sys.executable, str(launcher), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "Launch Camoufox server with sanitized config" in result.stdout


def test_camoufox_launcher_sets_proxy_identity_when_proxy_is_configured(monkeypatch):
    captured = {}

    def fake_launch_options(**options):
        captured.update(options)
        return options

    class FakeProcess:
        stdin = None

        def wait(self):
            return 0

    monkeypatch.setattr(settings, "proxy_server", "http://127.0.0.1:7890")
    monkeypatch.setattr(camoufox_launcher, "launch_options", fake_launch_options)
    monkeypatch.setattr(camoufox_launcher, "get_nodejs", lambda: sys.executable)
    monkeypatch.setattr(camoufox_launcher, "LAUNCH_SCRIPT", Path(__file__))
    monkeypatch.setattr(camoufox_launcher.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    try:
        camoufox_launcher.launch_camoufox_server(port=1234, headless=True)
    except RuntimeError as exc:
        assert "terminated unexpectedly" in str(exc)

    assert captured["proxy"] == {"server": "http://127.0.0.1:7890"}
    assert captured["locale"] == settings.camoufox_locale
    assert captured["config"]["timezone"] == settings.camoufox_timezone
    assert captured["config"]["geolocation:latitude"] == settings.camoufox_geolocation_latitude
    assert captured["config"]["geolocation:longitude"] == settings.camoufox_geolocation_longitude
    assert captured["i_know_what_im_doing"] is True