"""Image extraction and OCR helpers for review workflow."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OCR_PYTHON = os.getenv(
    "PADDLEOCR_PYTHON",
    os.path.join(BASE_DIR, ".venv_paddleocr", "bin", "python"),
)
OCR_WORKER = os.path.join(BASE_DIR, "core", "ocr_worker.py")


def _get_ocr_timeout_seconds() -> float:
    """读取 OCR 超时时间，默认走短超时，避免阻塞主审核流程。"""
    raw_timeout = os.getenv("OCR_TIMEOUT_SECONDS", "120")
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError:
        timeout_seconds = 120.0
    return max(0.0, timeout_seconds)


def run_ocr_on_images(image_paths: List[str]) -> Dict[str, Any]:
    """Run PaddleOCR out-of-process so main text audit remains isolated."""
    unique_paths = [path for path in dict.fromkeys(image_paths) if path]
    if not unique_paths:
        return {
            "texts": [],
            "merged_text": "",
            "errors": [],
            "available": True,
            "skip_reason": "",
        }

    timeout_seconds = _get_ocr_timeout_seconds()
    if timeout_seconds <= 0:
        reason = "OCR 已通过 OCR_TIMEOUT_SECONDS=0 禁用"
        logger.info(reason)
        return {
            "texts": [],
            "merged_text": "",
            "errors": [reason],
            "available": False,
            "skip_reason": reason,
        }

    if not os.path.exists(DEFAULT_OCR_PYTHON):
        error = f"OCR Python not found: {DEFAULT_OCR_PYTHON}"
        logger.warning(error)
        return {
            "texts": [],
            "merged_text": "",
            "errors": [error],
            "available": False,
            "skip_reason": error,
        }

    if not os.path.exists(OCR_WORKER):
        error = f"OCR worker not found: {OCR_WORKER}"
        logger.warning(error)
        return {
            "texts": [],
            "merged_text": "",
            "errors": [error],
            "available": False,
            "skip_reason": error,
        }

    try:
        env = os.environ.copy()
        env.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        completed = subprocess.run(
            [DEFAULT_OCR_PYTHON, OCR_WORKER, *unique_paths],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        reason = f"OCR subprocess timed out after {timeout_seconds:g} seconds"
        logger.warning("%s, skipping OCR", reason)
        return {
            "texts": [],
            "merged_text": "",
            "errors": [reason],
            "available": False,
            "skip_reason": reason,
        }
    except Exception as exc:  # pragma: no cover - subprocess failures are environmental
        error = f"OCR subprocess failed: {exc}"
        logger.error(error)
        return {
            "texts": [],
            "merged_text": "",
            "errors": [error],
            "available": False,
            "skip_reason": error,
        }

    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        error = f"OCR subprocess exited with {completed.returncode}: {stderr}"
        logger.error(error)
        return {
            "texts": [],
            "merged_text": "",
            "errors": [error],
            "available": False,
            "skip_reason": error,
        }

    try:
        parsed = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        error = f"OCR subprocess returned invalid JSON: {exc}"
        logger.error(error)
        return {
            "texts": [],
            "merged_text": "",
            "errors": [error],
            "available": False,
            "skip_reason": error,
        }

    image_texts = []
    merged_text_parts = []
    errors = []
    for item in parsed.get("results") or []:
        text = str(item.get("text") or "").strip()
        error = str(item.get("error") or "").strip()
        image_path = str(item.get("image_path") or "")
        image_texts.append({"image_path": image_path, "text": text, "error": error})
        if text:
            merged_text_parts.append(text)
        if error:
            errors.append(f"{os.path.basename(image_path) or image_path}: {error}")

    return {
        "texts": image_texts,
        "merged_text": "\n\n".join(merged_text_parts).strip(),
        "errors": errors,
        "available": True,
        "skip_reason": "",
    }
