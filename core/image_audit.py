"""Image extraction and OCR helpers for review workflow."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List

from PIL import Image, ImageFilter, ImageStat

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OCR_PYTHON = os.getenv(
    "PADDLEOCR_PYTHON",
    os.path.join(BASE_DIR, ".venv_paddleocr", "bin", "python"),
)
OCR_WORKER = os.path.join(BASE_DIR, "core", "ocr_worker.py")


def filter_images_for_text_check(image_paths: List[str]) -> Dict[str, Any]:
    """快速筛掉明显无字的图片，降低 OCR/LLM 负担。

    这是保守筛选：只跳过“极大概率没字”的图；拿不准就保留。
    """
    unique_paths = [path for path in dict.fromkeys(image_paths) if path]
    likely_text_paths: List[str] = []
    skipped_paths: List[str] = []
    errors: List[str] = []

    for image_path in unique_paths:
        try:
            with Image.open(image_path) as img:
                img = img.convert("L")
                width, height = img.size
                if width <= 0 or height <= 0:
                    skipped_paths.append(image_path)
                    continue

                # 大图缩小后做启发式判断，避免耗时过高。
                thumb = img.copy()
                thumb.thumbnail((640, 640))

                edges = thumb.filter(ImageFilter.FIND_EDGES)
                edge_mean = float(ImageStat.Stat(edges).mean[0])

                dark_pixels = 0
                total_pixels = thumb.width * thumb.height
                pixels = thumb.load()
                for y in range(thumb.height):
                    for x in range(thumb.width):
                        if pixels[x, y] < 170:
                            dark_pixels += 1
                dark_ratio = dark_pixels / max(total_pixels, 1)

                # 经验阈值：非常空、几乎没边缘、几乎没深色像素的图片，视为无字。
                if edge_mean < 6.0 and dark_ratio < 0.008:
                    skipped_paths.append(image_path)
                else:
                    likely_text_paths.append(image_path)
        except Exception as exc:
            # 拿不准的图片不跳过，保守起见继续走 OCR。
            likely_text_paths.append(image_path)
            errors.append(f"{os.path.basename(image_path) or image_path}: {exc}")

    return {
        "likely_text_paths": likely_text_paths,
        "skipped_paths": skipped_paths,
        "errors": errors,
    }


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
