import pytest
from unittest.mock import patch

from app import create_app
import app as app_module
import routes.api as api_module


@pytest.fixture
def client():
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def test_fill_approved_content_accepts_common_pass_statuses(client):
    task_id = "fill1234"
    api_module._tasks[task_id] = {
        "type": "feishu_url",
        "data": {
            "is_bitable": True,
            "app_token": "app_tok",
            "sheet_id": "tbl123",
            "headers": ["标题", "文案", "评论区文案", "小题审核状态", "稿件链接"],
            "data": [
                {
                    "_record_id": "rec1",
                    "小题审核状态": "已通过",
                    "稿件链接": {"link": "https://example.com/doc1", "text": "doc1"},
                }
            ],
        },
        "status": "ready",
    }

    snapshot_content = "标题：标题A\n文案：正文B\n评论区文案：评论C"

    class ImmediateThread:
        def __init__(self, target=None, args=None, daemon=None):
            self.target = target
            self.args = args or ()
        def start(self):
            self.target(*self.args)

    with patch("core.auth.load_config", return_value={"auth": {"enabled": False}}), \
         patch("routes.api.load_config", return_value={"feishu_app_id": "app_id", "feishu_app_secret": "app_secret"}), \
         patch("routes.api.download_feishu_doc_snapshot", return_value={"content": snapshot_content}), \
         patch("routes.api.threading.Thread", ImmediateThread), \
         patch("routes.api.write_bitable_records", return_value=True) as mock_write:
        resp = client.post(f"/api/fill-approved-content/{task_id}", json={})
        assert resp.status_code == 202
        status_resp = client.get(f"/api/fill-approved-content/{task_id}/status")

    data = status_resp.get_json()
    assert data["status"] == "completed"
    assert data["updated"] == 1
    assert data["skipped"] == 0
    mock_write.assert_called_once()
