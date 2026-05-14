import subprocess
from unittest.mock import patch

import core.image_audit as image_audit


def test_run_ocr_on_images_timeout_is_skipped(monkeypatch):
    monkeypatch.setenv("OCR_TIMEOUT_SECONDS", "5")

    with patch.object(image_audit.os.path, "exists", return_value=True), \
         patch.object(image_audit.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd=["ocr"], timeout=5)):
        result = image_audit.run_ocr_on_images(["/tmp/image_1.jpg", "/tmp/image_2.jpg"])

    assert result["available"] is False
    assert "skip_reason" in result
    assert "timed out" in result["skip_reason"]
    assert result["merged_text"] == ""
