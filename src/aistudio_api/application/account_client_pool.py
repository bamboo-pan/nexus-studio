"""Request-scoped AI Studio clients for stored accounts."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.cache.snapshot_cache import SnapshotCache
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.request_logs import RequestLogStore

logger = logging.getLogger("aistudio.account_pool")


@dataclass
class AccountClientEntry:
    account_id: str
    auth_file: str
    client: AIStudioClient


class AccountClientPool:
    """Maintains one isolated AIStudioClient per account."""

    def __init__(
        self,
        account_store: AccountStore,
        *,
        client_factory: Callable[..., AIStudioClient] = AIStudioClient,
        port: int = 9222,
        use_pure_http: bool = False,
        request_log_store: RequestLogStore | None = None,
    ) -> None:
        self._store = account_store
        self._client_factory = client_factory
        self._port = port
        self._use_pure_http = use_pure_http
        self._request_log_store = request_log_store
        self._clients: dict[str, AccountClientEntry] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, account_id: str) -> AIStudioClient | None:
        auth_path = self._store.get_auth_path(account_id)
        if auth_path is None:
            return None
        auth_file = str(auth_path)
        async with self._lock:
            entry = self._clients.get(account_id)
            if entry is not None and entry.auth_file == auth_file:
                return entry.client
            if entry is not None:
                await entry.client.close()

            client = self._client_factory(
                port=self._port,
                use_pure_http=self._use_pure_http,
                snapshot_cache=SnapshotCache(),
                request_log_store=self._request_log_store,
            )
            await client.switch_auth(auth_file)
            self._clients[account_id] = AccountClientEntry(
                account_id=account_id,
                auth_file=auth_file,
                client=client,
            )
            logger.info("Created isolated client for account=%s", account_id)
            return client

    async def invalidate(self, account_id: str) -> None:
        async with self._lock:
            entry = self._clients.pop(account_id, None)
        if entry is not None:
            await entry.client.close()

    async def close(self) -> None:
        async with self._lock:
            entries = list(self._clients.values())
            self._clients.clear()
        for entry in entries:
            await entry.client.close()
