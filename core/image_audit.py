"""Image extraction and OCR helpers for review workflow."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from PIL import Image, ImageFilter, ImageStat

from .config import load_config
from .volcengine_ocr import (
    run_ocr_on_images_with_volcengine,
    volcengine_config_ready,
)

logger = logging.getLogger(__name__)


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


def select_images_for_ocr(image_paths: List[str]) -> List[str]:
    """返回全部疑似含字图片，供后续 LLM 图片兜底使用。"""
    selected = [path for path in image_paths if path]
    logger.info(
        "Image selection for downstream checks: total_candidates=%s selected=%s",
        len(image_paths),
        len(selected),
    )
    if selected:
        logger.info("Selected image paths for downstream checks: %s", selected)
    return selected


def _get_ocr_provider(config: Dict[str, Any]) -> str:
    provider = str(os.getenv("OCR_PROVIDER") or config.get("ocr_provider") or "").strip().lower()
    if provider:
        return provider
    if volcengine_config_ready(config):
        return "volcengine"
    return "disabled"


def run_ocr_on_images(image_paths: List[str]) -> Dict[str, Any]:
    """Run OCR with the configured provider and keep the response shape stable."""
    unique_paths = [path for path in dict.fromkeys(image_paths) if path]
    logger.info("OCR request received: image_count=%s", len(unique_paths))
    if not unique_paths:
        return {
            "texts": [],
            "merged_text": "",
            "errors": [],
            "available": True,
            "skip_reason": "",
        }

    config = load_config()
    provider = _get_ocr_provider(config)
    logger.info("OCR provider selected: %s", provider)

    if provider in {"disabled", "off", "none"}:
        reason = "OCR 已禁用"
        logger.info("%s: provider=%s", reason, provider)
        return {
            "texts": [],
            "merged_text": "",
            "errors": [reason],
            "available": False,
            "skip_reason": reason,
        }

    if provider == "volcengine":
        return run_ocr_on_images_with_volcengine(unique_paths, config=config)

    reason = f"Unknown OCR provider: {provider}"
    logger.warning(reason)
    return {
        "texts": [],
        "merged_text": "",
        "errors": [reason],
        "available": False,
        "skip_reason": reason,
    }
