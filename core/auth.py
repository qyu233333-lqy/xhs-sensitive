"""认证与用户分组映射。"""

import json
import logging
import os
import secrets
from functools import wraps
from typing import Any, Dict
from urllib.parse import urlencode

import requests
from flask import g, jsonify, request, session

from .config import load_config

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_auth_config(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = config or load_config()
    return cfg.get("auth") or {}


def is_auth_enabled(config: Dict[str, Any] | None = None) -> bool:
    return bool(get_auth_config(config).get("enabled"))


def is_auth_ready(config: Dict[str, Any] | None = None) -> bool:
    auth_config = get_auth_config(config)
    return bool(
        auth_config.get("enabled")
        and auth_config.get("dingtalk_app_key")
        and auth_config.get("dingtalk_app_secret")
        and auth_config.get("dingtalk_redirect_uri")
    )


def _mapping_file_path(config: Dict[str, Any] | None = None) -> str:
    auth_config = get_auth_config(config)
    raw_path = auth_config.get("user_mapping_path") or "user_groups.json"
    return raw_path if os.path.isabs(raw_path) else os.path.join(BASE_DIR, raw_path)


def load_user_mappings(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    path = _mapping_file_path(config)
    if not os.path.exists(path):
        return {"users": []}

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("用户映射文件必须为 JSON object")
    users = data.get("users")
    if users is None:
        data["users"] = []
    elif not isinstance(users, list):
        raise ValueError("用户映射文件中的 users 必须为数组")
    return data


def _normalize_user_info(user_info: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {
        "userid": str(user_info.get("userid") or user_info.get("userId") or user_info.get("staffId") or "").strip(),
        "unionid": str(user_info.get("unionid") or user_info.get("unionId") or "").strip(),
        "openid": str(user_info.get("openid") or user_info.get("openId") or "").strip(),
        "mobile": str(user_info.get("mobile") or "").strip(),
        "email": str(user_info.get("email") or "").strip(),
        "nick": str(user_info.get("nick") or user_info.get("name") or user_info.get("displayName") or "").strip(),
    }
    normalized["display_name"] = normalized["nick"] or normalized["userid"] or normalized["unionid"] or "未知用户"
    return normalized


def _extract_match_fields(entry: Dict[str, Any]) -> Dict[str, str]:
    match_fields = entry.get("match") or entry.get("identifiers") or {}
    if not isinstance(match_fields, dict):
        match_fields = {}

    # 兼容更扁平的写法
    for key in ("userid", "unionid", "openid", "mobile", "email"):
        value = entry.get(key)
        if value and key not in match_fields:
            match_fields[key] = value

    return {k: str(v).strip() for k, v in match_fields.items() if str(v).strip()}


def _entry_matches_user(entry: Dict[str, Any], normalized_user: Dict[str, Any]) -> bool:
    match_fields = _extract_match_fields(entry)
    if not match_fields:
        return False
    for field_name, expected in match_fields.items():
        actual = str(normalized_user.get(field_name) or "").strip()
        if actual and actual == expected:
            return True
    return False


def resolve_user_access(user_info: Dict[str, Any], config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    normalized_user = _normalize_user_info(user_info)
    mappings = load_user_mappings(config).get("users", [])

    for entry in mappings:
        if not isinstance(entry, dict):
            continue
        if _entry_matches_user(entry, normalized_user):
            profile_id = str(entry.get("profile_id") or "").strip()
            if not profile_id:
                raise ValueError("匹配到的用户映射缺少 profile_id")
            return {
                "authenticated": True,
                "profile_id": profile_id,
                "profile_label": str(entry.get("profile_label") or profile_id).strip() or profile_id,
                "display_name": str(entry.get("display_name") or entry.get("name") or normalized_user["display_name"]).strip(),
                "is_admin": bool(entry.get("is_admin")),
                "identifiers": normalized_user,
            }

    logger.warning("DingTalk user is not mapped: %s", json.dumps(normalized_user, ensure_ascii=False))
    raise PermissionError("当前钉钉账号未分配到可用部门，请联系管理员维护 user_groups.json")


def get_current_user() -> Dict[str, Any] | None:
    user = session.get("auth_user")
    return user if isinstance(user, dict) else None


def build_dingtalk_login_url(config: Dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    auth_config = get_auth_config(cfg)
    state = secrets.token_urlsafe(24)
    session["dingtalk_oauth_state"] = state
    params = {
        "redirect_uri": auth_config.get("dingtalk_redirect_uri", ""),
        "response_type": "code",
        "client_id": auth_config.get("dingtalk_app_key", ""),
        "scope": auth_config.get("dingtalk_scope", "openid"),
        "state": state,
        "prompt": "consent",
    }
    return f"{auth_config.get('authorize_url')}?{urlencode(params)}"


def _request_json(method: str, url: str, **kwargs) -> Dict[str, Any]:
    resp = requests.request(method, url, timeout=15, **kwargs)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("errcode") not in (None, 0):
        raise ValueError(data.get("errmsg") or data.get("message") or "钉钉接口返回错误")
    return data


def exchange_code_for_user_token(code: str, config: Dict[str, Any] | None = None) -> str:
    auth_config = get_auth_config(config)
    payload = {
        "clientId": auth_config.get("dingtalk_app_key", ""),
        "clientSecret": auth_config.get("dingtalk_app_secret", ""),
        "code": code,
        "grantType": "authorization_code",
    }
    data = _request_json("POST", auth_config.get("user_access_token_url"), json=payload)
    access_token = data.get("accessToken") or data.get("access_token")
    if not access_token:
        raise ValueError("未从钉钉返回中获取到 user access token")
    return access_token


def fetch_dingtalk_user_info(user_access_token: str, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    auth_config = get_auth_config(config)
    headers = {
        "x-acs-dingtalk-access-token": user_access_token,
    }
    return _request_json("GET", auth_config.get("user_info_url"), headers=headers)


def login_required(admin: bool = False):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            config = load_config()
            if not is_auth_enabled(config):
                return func(*args, **kwargs)
            if not is_auth_ready(config):
                return jsonify({"error": "钉钉登录已启用，但配置不完整", "auth_required": True}), 503

            current_user = get_current_user()
            if not current_user:
                return jsonify({"error": "请先使用钉钉登录", "auth_required": True}), 401
            if admin and not current_user.get("is_admin"):
                return jsonify({"error": "需要管理员权限", "auth_required": True}), 403

            g.current_user = current_user
            return func(*args, **kwargs)

        return wrapper

    return decorator


def effective_profile_id(config: Dict[str, Any] | None = None, requested_profile_id: str | None = None) -> str:
    cfg = config or load_config()
    if is_auth_enabled(cfg):
        current_user = get_current_user()
        if not current_user:
            raise PermissionError("请先登录")
        return current_user.get("profile_id") or cfg.get("default_profile_id") or "ops1"
    return requested_profile_id or cfg.get("default_profile_id") or "ops1"


def get_auth_status(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = config or load_config()
    current_user = get_current_user()
    auth_config = get_auth_config(cfg)
    mapping_count = len(load_user_mappings(cfg).get("users", [])) if os.path.exists(_mapping_file_path(cfg)) else 0
    return {
        "enabled": bool(auth_config.get("enabled")),
        "ready": is_auth_ready(cfg),
        "authenticated": bool(current_user),
        "login_url": "/api/auth/dingtalk/login" if auth_config.get("enabled") else "",
        "logout_url": "/api/auth/logout",
        "mapping_path": auth_config.get("user_mapping_path", "user_groups.json"),
        "mapping_count": mapping_count,
        "user": {
            "display_name": current_user.get("display_name"),
            "profile_id": current_user.get("profile_id"),
            "profile_label": current_user.get("profile_label"),
            "is_admin": bool(current_user.get("is_admin")),
        } if current_user else None,
    }


def store_authenticated_user(auth_user: Dict[str, Any]) -> None:
    session.permanent = True
    session["auth_user"] = auth_user


def clear_authenticated_user() -> None:
    session.pop("auth_user", None)
    session.pop("dingtalk_oauth_state", None)


def build_callback_redirect(error: str | None = None) -> str:
    base = request.host_url.rstrip("/") + "/"
    if not error:
        return base
    return f"{base}?auth_error={error}"
