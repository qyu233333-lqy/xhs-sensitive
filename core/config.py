"""配置管理核心模块"""

import json
import os
import logging
from copy import deepcopy
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def _default_key_profiles() -> Dict[str, Any]:
    return {
        "ops1": {
            "label": "运营一部",
            "api_key": "",
            "base_url": "",
            "model": "claude-opus-4-6",
            "enabled": True,
        },
        "ops3": {
            "label": "运营三部",
            "api_key": "",
            "base_url": "",
            "model": "claude-opus-4-6",
            "enabled": True,
        }
    }


def _default_auth_config() -> Dict[str, Any]:
    return {
        "enabled": False,
        "provider": "dingtalk",
        "dingtalk_app_key": "",
        "dingtalk_app_secret": "",
        "dingtalk_redirect_uri": "",
        "dingtalk_scope": "openid",
        "authorize_url": "https://login.dingtalk.com/oauth2/auth",
        "user_access_token_url": "https://api.dingtalk.com/v1.0/oauth2/userAccessToken",
        "user_info_url": "https://api.dingtalk.com/v1.0/contact/users/me",
        "user_mapping_path": "user_groups.json",
    }


def _profile_env(profile_id: str, field_name: str) -> str:
    suffix = "".join(ch if ch.isalnum() else "_" for ch in profile_id.upper())
    mapping = {
        "api_key": f"ANTHROPIC_API_KEY_{suffix}",
        "base_url": f"API_BASE_URL_{suffix}",
        "model": f"CLAUDE_MODEL_{suffix}",
    }
    return mapping[field_name]


def _apply_nested_defaults(target: Dict[str, Any], defaults: Dict[str, Any]) -> None:
    for key, default_value in defaults.items():
        if key not in target:
            target[key] = deepcopy(default_value)
        elif isinstance(default_value, dict) and isinstance(target.get(key), dict):
            _apply_nested_defaults(target[key], default_value)


def load_config() -> Dict[str, Any]:
    """Load configuration from file and environment variables.
    Environment variables take precedence over config file."""
    config = {}

    # Load from config file first
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Failed to load config file: {e}")

    # Set default values for new project audit configurations
    defaults = {
        "session_secret": "",
        "default_profile_id": "ops1",
        "key_profiles": _default_key_profiles(),
        "auth": _default_auth_config(),
        "project_config_path": "ref.csv",
        "enable_project_audit": True,
        "audit_modes": {
            "hashtag_strict": True,
            "benefit_fuzzy": False,
            "slogan_exact": True
        },
        "project_audit_settings": {
            "auto_match_projects": True,
            "fallback_to_general_audit": True,
            "max_project_details": 5,
            "include_project_info_in_notes": True
        }
    }

    # Apply defaults for missing keys
    for key, default_value in defaults.items():
        if key not in config:
            config[key] = deepcopy(default_value)
        elif key == "key_profiles" and isinstance(config[key], dict):
            for profile_id, profile_default in default_value.items():
                if profile_id not in config[key]:
                    config[key][profile_id] = deepcopy(profile_default)
                elif isinstance(config[key][profile_id], dict):
                    for sub_key, sub_default in profile_default.items():
                        if sub_key not in config[key][profile_id]:
                            config[key][profile_id][sub_key] = sub_default
        elif key == "auth" and isinstance(config[key], dict):
            _apply_nested_defaults(config[key], default_value)
        elif key == "audit_modes" and isinstance(config[key], dict):
            # Merge audit_modes defaults
            for sub_key, sub_default in default_value.items():
                if sub_key not in config[key]:
                    config[key][sub_key] = sub_default
        elif key == "project_audit_settings" and isinstance(config[key], dict):
            # Merge project_audit_settings defaults
            for sub_key, sub_default in default_value.items():
                if sub_key not in config[key]:
                    config[key][sub_key] = sub_default

    # Override with environment variables if present
    env_mappings = {
        "ANTHROPIC_API_KEY": "api_key",
        "API_BASE_URL": "base_url",
        "CLAUDE_MODEL": "model",
        "FEISHU_APP_ID": "feishu_app_id",
        "FEISHU_APP_SECRET": "feishu_app_secret",
        "PROJECT_CONFIG_PATH": "project_config_path",
        "ENABLE_PROJECT_AUDIT": "enable_project_audit",
        "SESSION_SECRET": "session_secret",
    }

    for env_var, config_key in env_mappings.items():
        env_value = os.getenv(env_var)
        if env_value:
            if config_key == "enable_project_audit":
                config[config_key] = env_value.lower() in ('true', '1', 'yes', 'on')
            else:
                config[config_key] = env_value

    auth_env_mappings = {
        "DINGTALK_AUTH_ENABLED": ("enabled", "bool"),
        "DINGTALK_APP_KEY": ("dingtalk_app_key", "str"),
        "DINGTALK_APP_SECRET": ("dingtalk_app_secret", "str"),
        "DINGTALK_REDIRECT_URI": ("dingtalk_redirect_uri", "str"),
        "DINGTALK_SCOPE": ("dingtalk_scope", "str"),
        "DINGTALK_AUTHORIZE_URL": ("authorize_url", "str"),
        "DINGTALK_USER_ACCESS_TOKEN_URL": ("user_access_token_url", "str"),
        "DINGTALK_USER_INFO_URL": ("user_info_url", "str"),
        "DINGTALK_USER_MAPPING_PATH": ("user_mapping_path", "str"),
    }
    auth_config = config.get("auth") or {}
    for env_var, (config_key, value_type) in auth_env_mappings.items():
        env_value = os.getenv(env_var)
        if not env_value:
            continue
        auth_config[config_key] = env_value.lower() in ("true", "1", "yes", "on") if value_type == "bool" else env_value
    config["auth"] = auth_config

    for profile_id, profile in (config.get("key_profiles") or {}).items():
        if not isinstance(profile, dict):
            continue
        for field_name in ("api_key", "base_url", "model"):
            env_value = os.getenv(_profile_env(profile_id, field_name))
            if env_value:
                profile[field_name] = env_value

    return config


def get_key_profiles_metadata(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """返回可给前端使用的 profile 元数据，不包含密钥。"""
    profiles = []
    for profile_id, profile in (config.get("key_profiles") or {}).items():
        if not isinstance(profile, dict):
            continue
        if profile.get("enabled", True) is False:
            continue
        resolved_profile = resolve_ai_profile(config, profile_id)
        profiles.append({
            "id": profile_id,
            "label": profile.get("label", profile_id),
            "has_key": bool(resolved_profile.get("api_key")),
            "has_base_url": bool(resolved_profile.get("base_url")),
            "model": resolved_profile.get("model", ""),
        })
    return profiles


def resolve_ai_profile(config: Dict[str, Any], profile_id: Optional[str] = None) -> Dict[str, Any]:
    """按 profile_id 解析审核使用的 AI 配置。"""
    profiles = config.get("key_profiles") or {}
    selected_id = profile_id or config.get("default_profile_id") or "ops1"
    profile = profiles.get(selected_id) or {}

    api_key = profile.get("api_key") or config.get("api_key") or ""
    base_url = profile.get("base_url") or config.get("base_url") or ""
    model = profile.get("model") or config.get("model") or "claude-sonnet-4-20250514"

    return {
        "id": selected_id,
        "label": profile.get("label", selected_id),
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }


def get_safe_auth_metadata(config: Dict[str, Any]) -> Dict[str, Any]:
    """返回可给前端使用的认证配置元数据，不包含密钥。"""
    auth_config = config.get("auth") or {}
    return {
        "enabled": bool(auth_config.get("enabled")),
        "provider": auth_config.get("provider", "dingtalk"),
        "app_key": auth_config.get("dingtalk_app_key", ""),
        "has_app_key": bool(auth_config.get("dingtalk_app_key")),
        "has_app_secret": bool(auth_config.get("dingtalk_app_secret")),
        "redirect_uri": auth_config.get("dingtalk_redirect_uri", ""),
        "scope": auth_config.get("dingtalk_scope", "openid"),
        "user_mapping_path": auth_config.get("user_mapping_path", "user_groups.json"),
    }


def save_config(cfg: Dict[str, Any]) -> None:
    """Save configuration to file with error handling."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info("Configuration saved successfully")
    except (IOError, OSError) as e:
        logger.error(f"Failed to save configuration: {e}")
        raise
