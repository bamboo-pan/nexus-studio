from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx


def read_openai_compat_credentials() -> tuple[str, str]:
    credential_file = Path(os.environ.get("OPENAI_COMPAT_KEY_FILE", "/mnt/c/Users/bamboo/Documents/github/key.txt"))
    lines = [line.strip() for line in credential_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    base_url = os.environ.get("OPENAI_COMPAT_BASE_URL", "").strip()
    token = os.environ.get("OPENAI_COMPAT_API_KEY", "").strip()
    for line in lines:
        if line.startswith("OPENAI_BASE_URL="):
            base_url = line.split("=", 1)[1].strip()
        elif line.startswith("OPENAI_API_KEY="):
            token = line.split("=", 1)[1].strip()
        elif line.startswith(("http://", "https://")) and not base_url:
            base_url = line
        elif not token:
            token = line
    if token.startswith("Bearer "):
        token = token.removeprefix("Bearer ").strip()
    return (base_url or "https://api.openai.com/v1").rstrip("/"), token


def safe_proxy_value(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return "<set>"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", "", "", ""))


def safe_text(text: str, token: str) -> str:
    cleaned = str(text or "")
    if token:
        cleaned = cleaned.replace(token, "[REDACTED]")
    return cleaned[:500]


base_url, token = read_openai_compat_credentials()
models_url = f"{base_url}/models"
headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
parsed = urlsplit(base_url)
summary: dict[str, object] = {
    "base_scheme": parsed.scheme,
    "base_host": parsed.netloc,
    "base_path": parsed.path,
    "token_present": bool(token),
    "proxy_env": {},
    "tests": {},
}
for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
    value = os.environ.get(key)
    if value:
        summary["proxy_env"][key] = safe_proxy_value(value)  # type: ignore[index]

try:
    req = urllib.request.Request(models_url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read(500).decode("utf-8", errors="replace")
    summary["tests"]["urllib"] = {"ok": response.status < 400, "status": response.status, "body_prefix": safe_text(body, token)}  # type: ignore[index]
except Exception as exc:
    summary["tests"]["urllib"] = {"ok": False, "type": type(exc).__name__, "error": safe_text(str(exc), token)}  # type: ignore[index]

for name, kwargs in {
    "httpx_trust_env_true": {"trust_env": True},
    "httpx_trust_env_false": {"trust_env": False},
    "httpx_trust_env_true_verify_false": {"trust_env": True, "verify": False},
}.items():
    try:
        with httpx.Client(timeout=30, **kwargs) as client:
            response = client.get(models_url, headers=headers)
        summary["tests"][name] = {"ok": response.status_code < 400, "status": response.status_code, "http_version": response.http_version, "body_prefix": safe_text(response.text, token)}  # type: ignore[index]
    except Exception as exc:
        summary["tests"][name] = {"ok": False, "type": type(exc).__name__, "error": safe_text(str(exc), token)}  # type: ignore[index]

config_path = ""
try:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as config_file:
        config_file.write(f"url = {models_url}\n")
        config_file.write("request = GET\n")
        config_file.write("connect-timeout = 30\n")
        config_file.write("max-time = 60\n")
        config_file.write("silent\n")
        config_file.write("show-error\n")
        config_file.write("location\n")
        config_file.write("output = /dev/null\n")
        config_file.write("write-out = %{http_code} %{ssl_verify_result} %{http_version} %{remote_ip}\\n\n")
        config_file.write(f"header = Authorization: Bearer {token}\n")
        config_path = config_file.name
    proc = subprocess.run(["curl", "--config", config_path], text=True, capture_output=True, timeout=70)
    summary["tests"]["curl"] = {"ok": proc.returncode == 0 and proc.stdout.strip().split(" ", 1)[0].startswith("2"), "returncode": proc.returncode, "stdout": safe_text(proc.stdout.strip(), token), "stderr": safe_text(proc.stderr.strip(), token)}  # type: ignore[index]
except Exception as exc:
    summary["tests"]["curl"] = {"ok": False, "type": type(exc).__name__, "error": safe_text(str(exc), token)}  # type: ignore[index]
finally:
    if config_path:
        Path(config_path).unlink(missing_ok=True)

print(json.dumps(summary, ensure_ascii=False, indent=2))
