"""HTTP/CONNECT proxy and request dump helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("aistudio.proxy")

_latest_dump: dict = {
    "body": None,
    "cookies": None,
    "snapshot": None,
    "ts": 0,
}


def get_latest_snapshot() -> Optional[str]:
    return _latest_dump.get("snapshot")


def get_latest_cookies() -> Optional[str]:
    return _latest_dump.get("cookies")


def get_latest_body() -> Optional[list]:
    return _latest_dump.get("body")


class MITMProxy:
    def __init__(self, listen_port: int = 7861, target_host: str = "alkalimakersuite-pa.clients6.google.com"):
        self.listen_port = listen_port
        self.target_host = target_host

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return

            line = request_line.decode("utf-8", errors="replace").strip()
            method, path, _ = line.split(" ", 2)

            headers = {}
            while True:
                header_line = await reader.readline()
                if header_line in (b"\r\n", b"\n", b""):
                    break
                header = header_line.decode("utf-8", errors="replace").strip()
                if ":" in header:
                    key, value = header.split(":", 1)
                    headers[key.strip()] = value.strip()

            content_length = int(headers.get("Content-Length", 0))
            body = b""
            if content_length > 0:
                body = await reader.readexactly(content_length)

            if method == "CONNECT":
                await self._handle_connect(line, reader, writer)
                return

            await self._handle_http(method, path, headers, body, writer)
        except Exception as exc:
            logger.error("Handle client error: %s", exc)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_connect(self, line: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        host_port = line.split(" ")[1]
        host, port = (host_port.split(":") + ["443"])[:2]

        try:
            target_reader, target_writer = await asyncio.open_connection(host, int(port))
        except Exception:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        async def forward(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(forward(reader, target_writer), forward(target_reader, writer))

    async def _handle_http(
        self,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        writer: asyncio.StreamWriter,
    ):
        host = headers.get("Host", self.target_host)
        try:
            target_reader, target_writer = await asyncio.open_connection(host, 80)
        except Exception as exc:
            writer.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{exc}".encode())
            await writer.drain()
            writer.close()
            return

        request = f"{method} {path} HTTP/1.1\r\n"
        for key, value in headers.items():
            request += f"{key}: {value}\r\n"
        request += "\r\n"

        target_writer.write(request.encode() + body)
        await target_writer.drain()

        response = await target_reader.read(65536)
        writer.write(response)
        await writer.drain()

        target_writer.close()
        writer.close()

    async def start(self):
        server = await asyncio.start_server(self.handle_client, "127.0.0.1", self.listen_port)
        logger.info("MITM Proxy listening on 127.0.0.1:%s", self.listen_port)
        async with server:
            await server.serve_forever()


class GrpcDumpHandler:
    def __init__(self):
        self._latest: dict = {
            "body": None,
            "cookies": None,
            "snapshot": None,
            "headers": None,
            "ts": 0,
        }

    async def on_request(self, request):
        if "GenerateContent" not in request.url:
            return

        post_data = request.post_data
        if not post_data:
            return

        try:
            body = json.loads(post_data)
            if isinstance(body, list) and len(body) > 4:
                snapshot = body[4]
                if isinstance(snapshot, str) and snapshot.startswith("!"):
                    self._latest["snapshot"] = snapshot
                    self._latest["body"] = body
                    self._latest["ts"] = time.time()
                    logger.info("Captured snapshot: %s chars", len(snapshot))

                    all_headers = request.headers
                    cookie = all_headers.get("cookie", "")
                    if cookie:
                        self._latest["cookies"] = cookie
                    self._latest["headers"] = dict(all_headers)
        except Exception as exc:
            logger.error("Failed to parse request dump: %s", exc)

    def latest(self) -> dict:
        return dict(self._latest)

