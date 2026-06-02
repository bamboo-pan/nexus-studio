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


def test_camoufox_launcher_uses_compat_script_when_playwright_legacy_entry_is_missing(tmp_path, monkeypatch):
    node = tmp_path / "playwright" / "driver" / ("node.exe" if sys.platform == "win32" else "node")
    package_root = node.parent / "package"
    (package_root / "lib").mkdir(parents=True)
    (package_root / "lib" / "coreBundle.js").write_text("module.exports = {};", encoding="utf-8")
    node.write_text("node", encoding="utf-8")
    launch_script = tmp_path / "camoufox" / "launchServer.js"
    launch_script.parent.mkdir()
    launch_script.write_text("legacy", encoding="utf-8")
    launched = {}

    class FakeStdin:
        def __init__(self):
            self.data = ""
            self.closed = False

        def write(self, value):
            self.data += value

        def close(self):
            self.closed = True

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()

        def wait(self):
            return 0

    def fake_popen(cmd, cwd, stdin, text):
        process = FakeProcess()
        launched["cmd"] = cmd
        launched["cwd"] = cwd
        launched["stdin"] = stdin
        launched["text"] = text
        launched["process"] = process
        return process

    monkeypatch.setattr(settings, "proxy_server", None)
    monkeypatch.setattr(camoufox_launcher, "launch_options", lambda **options: options)
    monkeypatch.setattr(camoufox_launcher, "get_nodejs", lambda: str(node))
    monkeypatch.setattr(camoufox_launcher, "LAUNCH_SCRIPT", launch_script)
    monkeypatch.setattr(camoufox_launcher.subprocess, "Popen", fake_popen)

    try:
        camoufox_launcher.launch_camoufox_server(port=1234, headless=True)
    except RuntimeError as exc:
        assert "terminated unexpectedly" in str(exc)

    compat_script = Path(launched["cmd"][1])
    assert launched["cmd"][0] == str(node)
    assert compat_script != launch_script
    assert launched["cwd"] == package_root
    assert launched["stdin"] == camoufox_launcher.subprocess.PIPE
    assert launched["text"] is True
    compat_script_text = compat_script.read_text(encoding="utf-8")
    assert "coreBundle.js" in compat_script_text
    assert "BrowserServerLauncherImpl" in compat_script_text
    assert "envObjectToArray" in compat_script_text
    assert "ignoreAllDefaultArgs" in compat_script_text
    assert launched["process"].stdin.closed is True