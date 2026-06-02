"""Compatibility launcher for Camoufox Playwright server.

Camoufox's bundled `camoufox.server.launch_server()` currently forwards
`None`-valued options such as `proxy=None`, which breaks startup in some
versions with errors like `proxy: expected object, got null`.

This module launches the same underlying Node script, but prunes `None`
values first so the generated config matches what the browser expects.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SRC_ROOT = Path(__file__).resolve().parents[3]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import orjson
from camoufox.server import LAUNCH_SCRIPT, get_nodejs, to_camel_case_dict
from camoufox.utils import launch_options

from aistudio_api.config import camoufox_proxy_identity_options, settings


_COMPAT_LAUNCH_SERVER_JS = r"""
const { EventEmitter } = require('events');

function requireBrowserServerLauncher() {
    try {
        return require(`${process.cwd()}/lib/browserServerImpl.js`);
    } catch (error) {
        if (error.code !== 'MODULE_NOT_FOUND') throw error;
    }

    const core = require(`${process.cwd()}/lib/coreBundle.js`);
    const { remote, server, utils, iso } = core;

    class BrowserServerLauncherImpl {
        constructor(browserName) {
            this._browserName = browserName;
        }

        async launchServer(options = {}) {
            const playwright = server.createPlaywright({ sdkLanguage: 'javascript', isServer: true });
            const launchOptions = {
                ...options,
                ignoreDefaultArgs: Array.isArray(options.ignoreDefaultArgs) ? options.ignoreDefaultArgs : undefined,
                ignoreAllDefaultArgs: !!options.ignoreDefaultArgs && !Array.isArray(options.ignoreDefaultArgs),
                env: options.env ? envObjectToArray(options.env) : undefined,
                timeout: options.timeout ?? iso.DEFAULT_PLAYWRIGHT_LAUNCH_TIMEOUT,
            };
            let browser;
            if (options._userDataDir !== undefined) {
                const context = await playwright[this._browserName].launchPersistentContext(server.nullProgress, options._userDataDir, launchOptions);
                browser = context._browser;
            } else {
                browser = await playwright[this._browserName].launch(server.nullProgress, launchOptions);
            }
            const path = options.wsPath ? (options.wsPath.startsWith('/') ? options.wsPath : `/${options.wsPath}`) : `/${utils.createGuid()}`;
            const playwrightServer = new remote.PlaywrightServer({
                mode: options._sharedBrowser ? 'launchServerShared' : 'launchServer',
                path,
                maxConnections: Infinity,
                preLaunchedBrowser: browser,
            });
            const wsEndpoint = await playwrightServer.listen(options.port, options.host);
            const browserServer = new EventEmitter();
            const browserProcess = browser.options.browserProcess;
            browserServer.process = () => browserProcess.process;
            browserServer.wsEndpoint = () => wsEndpoint;
            browserServer.close = () => browserProcess.close();
            browserServer.kill = () => browserProcess.kill();
            browserServer._disconnectForTest = () => playwrightServer.close();
            browserProcess.onclose = (exitCode, signal) => {
                playwrightServer.close();
                browserServer.emit('close', exitCode, signal);
            };
            return browserServer;
        }
    }

    return { BrowserServerLauncherImpl };
}

const { BrowserServerLauncherImpl } = requireBrowserServerLauncher();

function envObjectToArray(env) {
    if (Array.isArray(env)) return env;
    const result = [];
    for (const name in env || {}) {
        if (env[name] !== undefined) result.push({ name, value: String(env[name]) });
    }
    return result;
}

function collectData() {
    return new Promise((resolve) => {
        let data = '';
        process.stdin.setEncoding('utf8');
        process.stdin.on('data', (chunk) => { data += chunk; });
        process.stdin.on('end', () => {
            resolve(JSON.parse(Buffer.from(data, 'base64').toString()));
        });
    });
}

collectData().then((options) => {
    console.time('Server launched');
    console.info('Launching server...');
    const launcher = new BrowserServerLauncherImpl('firefox');
    launcher.launchServer(options).then(browserServer => {
        console.timeEnd('Server launched');
        console.log('Websocket endpoint:\x1b[93m', browserServer.wsEndpoint(), '\x1b[0m');
        process.stdin.resume();
    }).catch(error => {
        console.error('Error launching server:', error.message);
        process.exit(1);
    });
}).catch((error) => {
    console.error('Error collecting data:', error.message);
    process.exit(1);
});
""".lstrip()


def _prune_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _prune_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_prune_none(item) for item in value]
    return value


def _playwright_package_root(nodejs: str | Path) -> Path:
    return Path(nodejs).parent / "package"


def _write_compat_launch_script() -> Path:
    script_path = Path(tempfile.gettempdir()) / "nexus-studio-camoufox-launchServer.js"
    try:
        current = script_path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if current != _COMPAT_LAUNCH_SERVER_JS:
        script_path.write_text(_COMPAT_LAUNCH_SERVER_JS, encoding="utf-8")
    return script_path


def _resolve_launch_script(nodejs: str | Path) -> Path:
    package_root = _playwright_package_root(nodejs)
    legacy_browser_server = package_root / "lib" / "browserServerImpl.js"
    bundled_core = package_root / "lib" / "coreBundle.js"
    if not legacy_browser_server.exists() and bundled_core.exists():
        return _write_compat_launch_script()
    return Path(LAUNCH_SCRIPT)


def launch_camoufox_server(*, port: int, headless: bool):
    options: dict[str, Any] = {"port": port, "headless": headless, "main_world_eval": True}
    if settings.proxy_server:
        options["proxy"] = {"server": settings.proxy_server}
        options.update(camoufox_proxy_identity_options())
    cfg = launch_options(**options)
    cfg = _prune_none(cfg)
    nodejs = get_nodejs()
    package_root = _playwright_package_root(nodejs)
    launch_script = _resolve_launch_script(nodejs)
    data = orjson.dumps(to_camel_case_dict(cfg))

    process = subprocess.Popen(
        [nodejs, str(launch_script)],
        cwd=package_root,
        stdin=subprocess.PIPE,
        text=True,
    )
    if process.stdin:
        process.stdin.write(base64.b64encode(data).decode())
        process.stdin.close()
    process.wait()
    raise RuntimeError("Server process terminated unexpectedly")


def main():
    parser = argparse.ArgumentParser(description="Launch Camoufox server with sanitized config")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    launch_camoufox_server(port=args.port, headless=args.headless)


if __name__ == "__main__":
    main()
