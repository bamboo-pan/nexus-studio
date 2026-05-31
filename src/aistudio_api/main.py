"""Unified CLI entrypoint for local development and installed usage."""

from __future__ import annotations

import argparse
import asyncio


def build_parser() -> argparse.ArgumentParser:
    from aistudio_api.config import settings

    parser = argparse.ArgumentParser(description="Nexus Studio unified entrypoint")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server_parser = subparsers.add_parser("server", help="启动 OpenAI 兼容 API 服务")
    server_parser.add_argument("--port", type=int, default=settings.port)
    server_parser.add_argument("--camoufox-port", type=int, default=settings.camoufox_port)

    client_parser = subparsers.add_parser("client", help="发送一次客户端请求")
    client_parser.add_argument("prompt", nargs="?", default="你好", help="用户消息")
    client_parser.add_argument("--model", "-m", help="模型名称")
    client_parser.add_argument("--system", "-s", help="系统指令")
    client_parser.add_argument("--search", action="store_true", help="启用 Google Search")
    client_parser.add_argument("--code", action="store_true", help="启用 Code Execution")
    client_parser.add_argument("--image", action="store_true", help="生图模式")
    client_parser.add_argument("--save", help="图片保存路径")
    client_parser.add_argument("--attach", "-a", nargs="+", help="附加图片（文件路径）")
    client_parser.add_argument("--port", type=int, default=settings.camoufox_port, help="Camoufox 调试端口")

    snapshot_parser = subparsers.add_parser("snapshot", help="抓取 snapshot")
    snapshot_parser.add_argument("prompt", nargs="?", default="你好，测试snapshot提取", help="触发用 prompt")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "server":
        from aistudio_api.api.app import main as server_main
        import sys

        sys.argv = ["nexus-studio-server", "--port", str(args.port), "--camoufox-port", str(args.camoufox_port)]
        server_main()
        return

    if args.command == "client":
        from aistudio_api.config import DEFAULT_TEXT_MODEL
        from aistudio_api.infrastructure.gateway.cli import _run_cli

        if not args.model:
            args.model = DEFAULT_TEXT_MODEL
        asyncio.run(_run_cli(args))
        return

    if args.command == "snapshot":
        from aistudio_api.infrastructure.browser.snapshot_extractor import SnapshotExtractor

        async def _run_snapshot():
            extractor = SnapshotExtractor()
            snap = await extractor.extract(args.prompt)
            cookies = extractor.get_cookies()
            print(f"snapshot: {len(snap)} 字符")
            print(f"cookies: {len(cookies or {})} 个")

        asyncio.run(_run_snapshot())
        return

