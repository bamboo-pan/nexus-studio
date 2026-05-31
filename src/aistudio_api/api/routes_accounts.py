"""账号管理路由。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Depends, Request, Response
from pydantic import BaseModel

from aistudio_api.api.dependencies import get_account_service, get_runtime_state
from aistudio_api.infrastructure.account.login_service import LoginStatus

router = APIRouter(prefix="/accounts")


class LoginStartRequest(BaseModel):
    name: str | None = None


class LoginStartResponse(BaseModel):
    session_id: str


class AccountResponse(BaseModel):
    id: str
    name: str
    email: str | None
    created_at: str
    last_used: str | None
    tier: str = "free"
    health_status: str = "unknown"
    health_reason: str | None = None
    last_health_check: str | None = None
    isolated_until: str | None = None


class LoginStatusResponse(BaseModel):
    session_id: str
    status: str
    account_id: str | None = None
    email: str | None = None
    error: str | None = None


class UpdateAccountRequest(BaseModel):
    name: str | None = None
    tier: str | None = None


class CredentialImportResponse(BaseModel):
    imported: list[AccountResponse]
    count: int


class AccountHealthResponse(BaseModel):
    ok: bool
    account: AccountResponse
    status: str
    reason: str | None = None
    tier: str
    last_health_check: str | None = None
    isolated_until: str | None = None


def _mark_sensitive_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _error_detail(message: str, error_type: str = "bad_request") -> dict[str, str]:
    return {"message": message, "type": error_type}


def _to_account_response(account) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        name=account.name,
        email=account.email,
        created_at=account.created_at,
        last_used=account.last_used,
        tier=getattr(account, "tier", "free"),
        health_status=getattr(account, "health_status", "unknown"),
        health_reason=getattr(account, "health_reason", None),
        last_health_check=getattr(account, "last_health_check", None),
        isolated_until=getattr(account, "isolated_until", None),
    )


def _account_response_from_dict(data: dict[str, Any]) -> AccountResponse:
    return AccountResponse(
        id=data["id"],
        name=data["name"],
        email=data.get("email"),
        created_at=data["created_at"],
        last_used=data.get("last_used"),
        tier=data.get("tier", "free"),
        health_status=data.get("health_status", "unknown"),
        health_reason=data.get("health_reason"),
        last_health_check=data.get("last_health_check"),
        isolated_until=data.get("isolated_until"),
    )


def _to_health_response(result: dict[str, Any]) -> AccountHealthResponse:
    return AccountHealthResponse(
        ok=bool(result.get("ok")),
        account=_account_response_from_dict(result["account"]),
        status=str(result.get("status") or "unknown"),
        reason=result.get("reason") if isinstance(result.get("reason"), str) else None,
        tier=str(result.get("tier") or "free"),
        last_health_check=result.get("last_health_check") if isinstance(result.get("last_health_check"), str) else None,
        isolated_until=result.get("isolated_until") if isinstance(result.get("isolated_until"), str) else None,
    )


@router.post("/login/start", response_model=LoginStartResponse)
async def login_start(
    req: LoginStartRequest,
    account_service=Depends(get_account_service),
):
    """启动 Google 登录流程。"""
    session_id = await account_service.start_login(req.name)
    return LoginStartResponse(session_id=session_id)


@router.get("/login/status/{session_id}", response_model=LoginStatusResponse)
async def login_status(
    session_id: str,
    account_service=Depends(get_account_service),
    runtime_state=Depends(get_runtime_state),
):
    """查询登录状态。"""
    session = account_service.get_login_status(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=_error_detail("登录会话不存在", "not_found"))
    if session.status == LoginStatus.COMPLETED and session.account_id and not session.auth_activated:
        auth_client = runtime_state.client
        if auth_client is not None:
            account = await account_service.activate_account(
                session.account_id,
                auth_client,
                runtime_state.snapshot_cache,
                runtime_state.busy_lock,
            )
            if account is None:
                session.status = LoginStatus.FAILED
                session.error = "登录已保存，但切换浏览器认证状态失败"
            else:
                session.auth_activated = True
                account_client_pool = getattr(runtime_state, "account_client_pool", None)
                if account_client_pool is not None:
                    await account_client_pool.invalidate(session.account_id)
        else:
            session.auth_activated = True
    return LoginStatusResponse(
        session_id=session.session_id,
        status=session.status.value,
        account_id=session.account_id,
        email=session.email,
        error=session.error,
    )


@router.get("", response_model=list[AccountResponse])
async def list_accounts(
    account_service=Depends(get_account_service),
):
    """列出所有账号。"""
    accounts = account_service.list_accounts()
    return [
        _to_account_response(a)
        for a in accounts
    ]


@router.get("/active", response_model=AccountResponse)
async def get_active_account(
    account_service=Depends(get_account_service),
):
    """获取当前活跃账号。"""
    account = account_service.get_active_account()
    if account is None:
        raise HTTPException(status_code=404, detail=_error_detail("没有活跃账号", "not_found"))
    return _to_account_response(account)


@router.get("/export")
async def export_all_credentials(
    response: Response,
    account_service=Depends(get_account_service),
) -> dict[str, Any]:
    """导出所有账号凭证备份包。"""
    _mark_sensitive_response(response)
    try:
        return account_service.export_credentials()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(f"账号 {exc} 缺少 auth.json，无法导出")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.get("/{account_id}/export")
async def export_account_credentials(
    account_id: str,
    response: Response,
    account_service=Depends(get_account_service),
) -> dict[str, Any]:
    """导出单个账号凭证备份包。"""
    _mark_sensitive_response(response)
    try:
        return account_service.export_credentials(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error_detail("账号不存在", "not_found")) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(f"账号 {exc} 缺少 auth.json，无法导出")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc


@router.post("/import", response_model=CredentialImportResponse)
async def import_credentials(
    request: Request,
    name: str | None = None,
    activate: bool = True,
    account_service=Depends(get_account_service),
    runtime_state=Depends(get_runtime_state),
):
    """导入凭证备份包或单账号 Playwright storage state。"""
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=_error_detail("无效的 JSON 凭证内容")) from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=_error_detail("凭证内容必须是 JSON 对象"))

    try:
        imported = account_service.import_credentials(payload, name=name, activate=activate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc

    account_client_pool = getattr(runtime_state, "account_client_pool", None)
    if account_client_pool is not None:
        for account in imported:
            await account_client_pool.invalidate(account.id)

    return CredentialImportResponse(
        imported=[_to_account_response(account) for account in imported],
        count=len(imported),
    )


@router.post("/{account_id}/activate", response_model=AccountResponse)
async def activate_account(
    account_id: str,
    account_service=Depends(get_account_service),
    runtime_state=Depends(get_runtime_state),
):
    """切换到指定账号。"""
    # 从 runtime_state 获取客户端、snapshot_cache、busy_lock
    auth_client = runtime_state.client
    snapshot_cache = runtime_state.snapshot_cache
    busy_lock = runtime_state.busy_lock

    if auth_client is None:
        raise HTTPException(status_code=503, detail=_error_detail("服务未就绪", "service_unavailable"))

    account = await account_service.activate_account(
        account_id, auth_client, snapshot_cache, busy_lock
    )
    if account is None:
        raise HTTPException(status_code=404, detail=_error_detail("账号不存在或切换失败", "not_found"))
    account_client_pool = getattr(runtime_state, "account_client_pool", None)
    if account_client_pool is not None:
        await account_client_pool.invalidate(account.id)
    return _to_account_response(account)


@router.post("/{account_id}/test", response_model=AccountHealthResponse)
async def test_account(
    account_id: str,
    account_service=Depends(get_account_service),
    runtime_state=Depends(get_runtime_state),
):
    """执行非破坏性的账号健康检查。"""
    session = getattr(runtime_state.client, "_session", None) if runtime_state.client is not None else None
    tier_detector = None
    if session is not None and hasattr(session, "detect_tier_for_auth_file"):
        async def tier_detector(auth_path):
            return await session.detect_tier_for_auth_file(str(auth_path))

    result = await account_service.test_account_with_tier(
        account_id,
        tier_detector=tier_detector,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=_error_detail("账号不存在", "not_found"))
    return _to_health_response(result)


@router.delete("/{account_id}")
async def delete_account(
    account_id: str,
    account_service=Depends(get_account_service),
    runtime_state=Depends(get_runtime_state),
):
    """删除账号。"""
    success = account_service.delete_account(account_id)
    if not success:
        raise HTTPException(status_code=404, detail=_error_detail("账号不存在", "not_found"))
    account_client_pool = getattr(runtime_state, "account_client_pool", None)
    if account_client_pool is not None:
        await account_client_pool.invalidate(account_id)
    return {"ok": True}


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: str,
    req: UpdateAccountRequest,
    account_service=Depends(get_account_service),
):
    """更新账号名称。"""
    if req.name is None and req.tier is None:
        raise HTTPException(status_code=400, detail=_error_detail("name or tier is required"))
    try:
        account = account_service.update_account(account_id, req.name, req.tier)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(str(exc))) from exc
    if account is None:
        raise HTTPException(status_code=404, detail=_error_detail("账号不存在", "not_found"))
    return _to_account_response(account)
