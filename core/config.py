"""配置管理核心模块"""

import json
import os
import logging
from typing import Dict, Any

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
        }
    }


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
        "default_profile_id": "ops1",
        "key_profiles": _default_key_profiles(),
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
            config[key] = default_value
        elif key == "key_profiles" and isinstance(config[key], dict):
            for profile_id, profile_default in default_value.items():
                if profile_id not in config[key]:
                    config[key][profile_id] = profile_default
                elif isinstance(config[key][profile_id], dict):
                    for sub_key, sub_default in profile_default.items():
                        if sub_key not in config[key][profile_id]:
                            config[key][profile_id][sub_key] = sub_default
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
    }

    for env_var, config_key in env_mappings.items():
        env_value = os.getenv(env_var)
        if env_value:
            if config_key == "enable_project_audit":
                config[config_key] = env_value.lower() in ('true', '1', 'yes', 'on')
            else:
                config[config_key] = env_value

    return config


def get_key_profiles_metadata(config: Dict[str, Any]) -> list[Dict[str, Any]]:
    """返回可给前端使用的 profile 元数据，不包含密钥。"""
    profiles = []
    for profile_id, profile in (config.get("key_profiles") or {}).items():
        if not isinstance(profile, dict):
            continue
        if profile.get("enabled", True) is False:
            continue
        profiles.append({
            "id": profile_id,
            "label": profile.get("label", profile_id),
            "has_key": bool(profile.get("api_key")),
            "has_base_url": bool(profile.get("base_url")),
            "model": profile.get("model", ""),
        })
    return profiles


def resolve_ai_profile(config: Dict[str, Any], profile_id: str | None = None) -> Dict[str, Any]:
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


def save_config(cfg: Dict[str, Any]) -> None:
    """Save configuration to file with error handling."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info("Configuration saved successfully")
    except (IOError, OSError) as e:
        logger.error(f"Failed to save configuration: {e}")
        raise
