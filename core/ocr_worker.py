#!/usr/bin/env python3
"""Standalone PaddleOCR worker executed inside the OCR virtualenv."""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def _build_ocr():
    from paddleocr import PaddleOCR

    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    base_dir = os.path.expanduser("~/.paddlex/official_models")
    return PaddleOCR(
        text_detection_model_name="PP-OCRv5_server_det",
        text_detection_model_dir=os.path.join(base_dir, "PP-OCRv5_server_det"),
        text_recognition_model_name="PP-OCRv5_server_rec",
        text_recognition_model_dir=os.path.join(base_dir, "PP-OCRv5_server_rec"),
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        device="cpu",
    )


def _extract_text(result: Any) -> str:
    texts: list[str] = []

    if hasattr(result, "res") and isinstance(result.res, dict):
        for item in result.res.get("rec_texts") or []:
            if item:
                texts.append(str(item).strip())
    elif isinstance(result, dict):
        for item in result.get("rec_texts") or []:
            if item:
                texts.append(str(item).strip())

    return "\n".join([text for text in texts if text])


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "missing image paths"}, ensure_ascii=False))
        return 1

    ocr = _build_ocr()
    payload = []
    for image_path in sys.argv[1:]:
        try:
            prediction = ocr.predict(image_path)
            merged = "\n".join(
                [piece for piece in (_extract_text(item) for item in prediction) if piece]
            ).strip()
            payload.append({"image_path": image_path, "text": merged, "error": ""})
        except Exception as exc:  # pragma: no cover - defensive bridge
            payload.append({"image_path": image_path, "text": "", "error": str(exc)})

    print(json.dumps({"results": payload}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
