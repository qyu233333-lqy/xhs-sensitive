"""Volcengine MultiLanguageOCR integration for image text extraction."""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional

from .config import load_config

logger = logging.getLogger(__name__)


def _load_sdk():
    try:
        from volcengine.ApiInfo import ApiInfo
        from volcengine.Credentials import Credentials
        from volcengine.ServiceInfo import ServiceInfo
        from volcengine.base.Service import Service
    except ImportError as exc:  # pragma: no cover - depends on runtime env
        raise RuntimeError(
            "Volcengine SDK is not installed. Run `pip install volcengine`."
        ) from exc

    return ApiInfo, Credentials, ServiceInfo, Service


def _get_volcengine_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = config or load_config()
    volc_cfg = dict(config.get("volcengine") or {})

    access_key = os.getenv("VOLCENGINE_ACCESS_KEY") or volc_cfg.get("access_key") or ""
    secret_key = os.getenv("VOLCENGINE_SECRET_KEY") or volc_cfg.get("secret_key") or ""
    region = os.getenv("VOLCENGINE_REGION") or volc_cfg.get("region") or "cn-north-1"
    service = os.getenv("VOLCENGINE_SERVICE") or volc_cfg.get("service") or "cv"
    host = os.getenv("VOLCENGINE_VISUAL_HOST") or volc_cfg.get("host") or "visual.volcengineapi.com"
    action = volc_cfg.get("ocr_action") or "MultiLanguageOCR"
    version = volc_cfg.get("ocr_version") or "2022-08-31"
    mode = str(volc_cfg.get("mode") or "text_block")
    filter_thresh = str(volc_cfg.get("filter_thresh") or "80")
    approximate_pixel = str(volc_cfg.get("approximate_pixel") or "0")

    return {
        "access_key": access_key,
        "secret_key": secret_key,
        "region": region,
        "service": service,
        "host": host,
        "action": action,
        "version": version,
        "mode": mode,
        "filter_thresh": filter_thresh,
        "approximate_pixel": approximate_pixel,
    }


def volcengine_config_ready(config: Optional[Dict[str, Any]] = None) -> bool:
    volc_cfg = _get_volcengine_config(config)
    return bool(volc_cfg["access_key"] and volc_cfg["secret_key"])


def _build_service(config: Optional[Dict[str, Any]] = None):
    ApiInfo, Credentials, ServiceInfo, Service = _load_sdk()
    volc_cfg = _get_volcengine_config(config)

    if not volc_cfg["access_key"] or not volc_cfg["secret_key"]:
        raise RuntimeError("Missing Volcengine access key or secret key")

    service_info = ServiceInfo(
        volc_cfg["host"],
        {"Content-Type": "application/x-www-form-urlencoded"},
        Credentials(
            volc_cfg["access_key"],
            volc_cfg["secret_key"],
            volc_cfg["service"],
            volc_cfg["region"],
        ),
        5,
        20,
        "https",
    )
    api_info = {
        "multi_language_ocr": ApiInfo(
            "POST",
            "/",
            {"Action": volc_cfg["action"], "Version": volc_cfg["version"]},
            {},
            {},
        )
    }
    return Service(service_info, api_info), volc_cfg


def _encode_image_base64(image_path: str) -> str:
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def _parse_ocr_response(raw_text: str, image_path: str) -> Dict[str, Any]:
    payload = json.loads(raw_text)
    if payload.get("code") != 10000:
        if payload.get("code") == 50400:
            raise PermissionError(
                "Volcengine OCR access denied (50400). "
                "Check that this IAM user has permission to call the cv service "
                "for MultiLanguageOCR."
            )
        raise RuntimeError(
            f"Volcengine OCR failed: code={payload.get('code')} message={payload.get('message')}"
        )

    ocr_infos = ((payload.get("data") or {}).get("ocr_infos")) or []
    texts = [str(item.get("text") or "").strip() for item in ocr_infos if str(item.get("text") or "").strip()]
    return {
        "image_path": image_path,
        "text": "\n".join(texts).strip(),
        "error": "",
    }


def run_ocr_on_images_with_volcengine(
    image_paths: List[str],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    unique_paths = [path for path in dict.fromkeys(image_paths) if path]
    if not unique_paths:
        return {
            "texts": [],
            "merged_text": "",
            "errors": [],
            "available": True,
            "skip_reason": "",
        }

    try:
        service, volc_cfg = _build_service(config)
    except Exception as exc:
        error = str(exc)
        logger.warning("Volcengine OCR unavailable: %s", error)
        return {
            "texts": [],
            "merged_text": "",
            "errors": [error],
            "available": False,
            "skip_reason": error,
        }

    image_texts: List[Dict[str, str]] = []
    merged_text_parts: List[str] = []
    errors: List[str] = []
    permission_denied = False

    for image_path in unique_paths:
        try:
            form = {
                "image_base64": _encode_image_base64(image_path),
                "mode": volc_cfg["mode"],
                "filter_thresh": volc_cfg["filter_thresh"],
                "approximate_pixel": volc_cfg["approximate_pixel"],
            }
            raw_text = service.post("multi_language_ocr", {}, form)
            result = _parse_ocr_response(raw_text, image_path)
            image_texts.append(result)
            if result["text"]:
                merged_text_parts.append(result["text"])
        except PermissionError as exc:
            permission_denied = True
            error = str(exc)
            errors.append(error)
            image_texts.append({"image_path": image_path, "text": "", "error": error})
            logger.warning("Volcengine OCR permission denied: %s", error)
            break
        except Exception as exc:  # pragma: no cover - remote service failures are environmental
            error = f"{os.path.basename(image_path) or image_path}: {exc}"
            errors.append(error)
            image_texts.append({"image_path": image_path, "text": "", "error": str(exc)})
            logger.warning("Volcengine OCR image failed: %s", error)

    if permission_denied:
        reason = (
            "Volcengine OCR 权限不足（50400 Access Denied）。"
            "请为当前 IAM 用户授予 cv/文字识别调用权限后重试。"
        )
        return {
            "texts": image_texts,
            "merged_text": "",
            "errors": errors,
            "available": False,
            "skip_reason": reason,
        }

    return {
        "texts": image_texts,
        "merged_text": "\n\n".join(part for part in merged_text_parts if part).strip(),
        "errors": errors,
        "available": True,
        "skip_reason": "",
    }
