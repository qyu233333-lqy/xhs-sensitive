#!/usr/bin/env python3
"""内容审核 Agent 测试套件"""

import csv
import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

# 确保在导入 app 前 mock 掉可能触发的外部调用
os.environ.setdefault("TESTING", "1")

import app as app_module
from app import (
    app,
    col_to_letter,
    cell_to_text,
    cell_to_url,
    review_one,
    feishu_request,
    _process_row,
    _write_back_feishu,
    get_rules,
    load_project_configs,
    get_project_config,
    validate_project_config,
    clear_project_configs_cache,
    extract_content_elements,
    extract_content_with_fallback,
    check_hashtags_llm,
    check_benefits_llm,
    check_slogans_llm,
    project_specific_review_llm,
    enhanced_review_one,
    get_project_config_for_review,
)


# ─── Fixtures ───

@pytest.fixture
def client():
    """Flask 测试客户端"""
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c

@pytest.fixture
def mock_config():
    """mock load_config 返回测试配置"""
    cfg = {
        "api_key": "sk-test1234567890abcdef",
        "base_url": "https://test-api.example.com",
        "model": "claude-sonnet-4-6",
        "feishu_app_id": "cli_test123",
        "feishu_app_secret": "secret_test456",
    }
    with patch.object(app_module, "load_config", return_value=cfg):
        yield cfg

@pytest.fixture
def mock_feishu_token():
    """mock feishu_token 返回假 token"""
    with patch.object(app_module, "feishu_token", return_value="fake-token-123"):
        yield "fake-token-123"

@pytest.fixture(autouse=True)
def clean_tasks():
    """每个测试后清理全局 tasks"""
    yield
    app_module.tasks.clear()


def _make_anthropic_response(text):
    """构造 mock 的 Anthropic API 响应"""
    mock_msg = MagicMock()
    mock_block = MagicMock()
    mock_block.text = text
    mock_msg.content = [mock_block]
    return mock_msg


def _parse_sse(data_bytes):
    """解析 SSE 响应为事件列表"""
    text = data_bytes.decode("utf-8") if isinstance(data_bytes, bytes) else data_bytes
    events = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


# ─── 纯函数测试 ───

class TestColToLetter:
    def test_single_letter_a(self):
        assert col_to_letter(0) == "A"

    def test_single_letter_z(self):
        assert col_to_letter(25) == "Z"

    def test_double_letter_aa(self):
        assert col_to_letter(26) == "AA"

    def test_double_letter_ab(self):
        assert col_to_letter(27) == "AB"

    def test_double_letter_az(self):
        assert col_to_letter(51) == "AZ"

    def test_double_letter_ba(self):
        assert col_to_letter(52) == "BA"

    def test_triple_letter(self):
        assert col_to_letter(702) == "AAA"

    def test_j_column(self):
        """实际使用：AI审核列是第10列 (index 9)"""
        assert col_to_letter(9) == "J"


class TestCellToText:
    def test_none(self):
        assert cell_to_text(None) == ""

    def test_int(self):
        assert cell_to_text(42) == "42"

    def test_float(self):
        assert cell_to_text(3.14) == "3.14"

    def test_string(self):
        assert cell_to_text("  hello  ") == "hello"

    def test_empty_string(self):
        assert cell_to_text("") == ""

    def test_rich_text_list(self):
        cell = [{"text": "hello"}, {"text": " world"}]
        assert cell_to_text(cell) == "hello world"

    def test_nested_list(self):
        cell = [[{"text": "nested"}]]
        assert cell_to_text(cell) == "nested"

    def test_dict_with_text(self):
        cell = {"text": "value", "link": "http://example.com"}
        assert cell_to_text(cell) == "value"

    def test_dict_empty_text(self):
        cell = {"text": ""}
        assert cell_to_text(cell) == ""

    def test_list_with_non_dict(self):
        cell = ["plain string"]
        assert cell_to_text(cell) == ""

    def test_other_type(self):
        assert cell_to_text(True) == "True"


class TestCellToUrl:
    def test_none(self):
        assert cell_to_url(None) == ""

    def test_list_with_link(self):
        cell = [{"text": "doc", "link": "https://xx.feishu.cn/wiki/abc123", "type": "url"}]
        assert cell_to_url(cell) == "https://xx.feishu.cn/wiki/abc123"

    def test_list_no_link(self):
        cell = [{"text": "plain text"}]
        assert cell_to_url(cell) == ""

    def test_nested_list_with_link(self):
        cell = [[{"text": "doc", "link": "https://xx.feishu.cn/docx/xyz"}]]
        assert cell_to_url(cell) == "https://xx.feishu.cn/docx/xyz"

    def test_dict_with_link(self):
        cell = {"link": "https://xx.feishu.cn/wiki/abc"}
        assert cell_to_url(cell) == "https://xx.feishu.cn/wiki/abc"

    def test_dict_with_url_key(self):
        cell = {"url": "https://xx.feishu.cn/docx/abc"}
        assert cell_to_url(cell) == "https://xx.feishu.cn/docx/abc"

    def test_fallback_url_extract(self):
        cell = "请查看 https://ai.feishu.cn/wiki/test123 这个文档"
        assert cell_to_url(cell) == "https://ai.feishu.cn/wiki/test123"

    def test_no_url(self):
        cell = "just plain text"
        assert cell_to_url(cell) == ""

    def test_non_feishu_url(self):
        cell = "https://google.com/search"
        assert cell_to_url(cell) == ""


# ─── AI 审核测试 ───

class TestReviewOne:
    def _make_client(self, response_text):
        client = MagicMock()
        client.messages.create.return_value = _make_anthropic_response(response_text)
        return client

    def test_passed(self):
        resp_json = json.dumps({
            "passed": True,
            "reason": "内容合规",
            "violations": [],
            "violation_quotes": []
        })
        client = self._make_client(resp_json)
        rules = {"_common": "test rules"}
        result = review_one(client, "test-model", rules, "good content", "user1", "南京大牌档")
        assert result["passed"] is True
        assert result["violations"] == []

    def test_failed(self):
        resp_json = json.dumps({
            "passed": False,
            "reason": "包含违禁词",
            "violations": ["使用了'最好'等绝对化用语"],
            "violation_quotes": ["这是最好的产品"]
        })
        client = self._make_client(resp_json)
        rules = {"_common": "no absolutes"}
        result = review_one(client, "test-model", rules, "这是最好的产品", "user2")
        assert result["passed"] is False
        assert len(result["violations"]) == 1

    def test_malformed_json_with_extra_text(self):
        text = '好的，以下是审核结果：\n{"passed": true, "reason": "ok", "violations": [], "violation_quotes": []}\n以上'
        client = self._make_client(text)
        result = review_one(client, "m", {"_common": ""}, "content", "name")
        assert result["passed"] is True

    def test_completely_invalid(self):
        client = self._make_client("I cannot process this request")
        result = review_one(client, "m", {"_common": ""}, "content", "name")
        assert result["passed"] is False
        assert "格式异常" in result["reason"]

    def test_category_rules_in_prompt(self):
        """验证分类规则被正确传入 prompt"""
        client = self._make_client('{"passed": true, "reason": "ok", "violations": [], "violation_quotes": []}')
        rules = {
            "_common": "通用规则",
            "南京大牌档": "话题词：#美团黑钻会员"
        }
        review_one(client, "m", rules, "content", "name", "南京大牌档")

        # 验证 AI 收到的 prompt 包含分类规则
        call_args = client.messages.create.call_args
        user_content = call_args[1]["messages"][0]["content"]
        assert "南京大牌档" in user_content
        assert "话题词" in user_content
        assert "通用规则" in user_content

    def test_fuzzy_category_match(self):
        """权益类型包含关键字时也能匹配"""
        client = self._make_client('{"passed": true, "reason": "ok", "violations": [], "violation_quotes": []}')
        rules = {"_common": "", "黑钻奔驰试驾": "回搜词：黑钻会员666"}
        review_one(client, "m", rules, "content", "name", "黑钻奔驰试驾预热")

        user_content = client.messages.create.call_args[1]["messages"][0]["content"]
        assert "黑钻会员666" in user_content

    def test_rules_as_string_fallback(self):
        """兼容旧格式：rules 为纯字符串"""
        client = self._make_client('{"passed": true, "reason": "ok", "violations": [], "violation_quotes": []}')
        review_one(client, "m", "plain string rules", "content", "name")
        user_content = client.messages.create.call_args[1]["messages"][0]["content"]
        assert "plain string rules" in user_content


# ─── Flask 路由测试 ───

class TestConfigRoutes:
    def test_get_config_masks_secrets(self, client, mock_config):
        resp = client.get("/api/config")
        data = resp.get_json()
        assert "api_key" not in data
        assert "feishu_app_secret" not in data
        assert "api_key_display" in data
        assert "***" in data["api_key_display"]
        assert data.get("feishu_secret_display") is not None

    def test_set_config(self, client):
        with patch.object(app_module, "load_config", return_value={}), \
             patch.object(app_module, "save_config") as mock_save:
            resp = client.post("/api/config",
                json={"base_url": "https://new.api.com", "model": "claude-haiku-4-5-20251001"},
                content_type="application/json")
            assert resp.status_code == 200
            assert resp.get_json()["ok"] is True
            mock_save.assert_called_once()

    def test_set_config_clears_feishu_cache(self, client):
        app_module._feishu_token_cache["token"] = "old-token"
        app_module._feishu_token_cache["expire"] = 9999999999
        with patch.object(app_module, "load_config", return_value={}), \
             patch.object(app_module, "save_config"):
            client.post("/api/config", json={"base_url": "x"}, content_type="application/json")
        assert app_module._feishu_token_cache["token"] is None


class TestParseUrl:
    def test_empty_url(self, client):
        resp = client.post("/api/parse-url", json={"url": ""}, content_type="application/json")
        assert resp.status_code == 400

    def test_non_feishu_url(self, client):
        resp = client.post("/api/parse-url", json={"url": "https://google.com/sheets/abc"}, content_type="application/json")
        assert resp.status_code == 400
        assert "feishu" in resp.get_json()["error"]

    def test_valid_url(self, client):
        fake_parsed = {
            "source": "feishu",
            "spreadsheet_token": "tok123",
            "sheet_id": "sid1",
            "sheet_title": "执行大表",
            "header_row_idx": 1,
            "headers": ["昵称", "稿件链接", "AI审核"],
            "col_map": {"昵称": 0, "稿件链接": 1, "AI审核": 2},
            "data_rows": [
                {"name": "User1", "url": "", "link_text": "doc", "existing_ai": "", "row_1indexed": 3, "category": "南京大牌档"},
                {"name": "User2", "url": "", "link_text": "doc2", "existing_ai": "已过审", "row_1indexed": 4, "category": "南京大牌档"},
            ],
        }
        with patch.object(app_module, "fetch_feishu_sheet", return_value=(fake_parsed, None)):
            resp = client.post("/api/parse-url",
                json={"url": "https://my.feishu.cn/sheets/abc123"},
                content_type="application/json")
            data = resp.get_json()
            assert resp.status_code == 200
            assert data["total"] == 2
            assert data["new_count"] == 1
            assert data["sheet"] == "执行大表"
            assert "task_id" in data


class TestRunReview:
    def test_unknown_task_id(self, client):
        resp = client.get("/api/review/nonexistent")
        assert resp.status_code == 404

    def test_no_api_key(self, client):
        app_module.tasks["t1"] = {
            "parsed": {"data_rows": [], "col_map": {}, "spreadsheet_token": "x"},
            "source": "feishu",
            "status": "parsed",
            "total": 0,
        }
        with patch.object(app_module, "load_config", return_value={"api_key": ""}):
            resp = client.get("/api/review/t1")
            events = _parse_sse(resp.data)
            assert events[0]["type"] == "error"
            assert "API Key" in events[0]["msg"]

    def test_review_skips_existing(self, client, mock_config):
        app_module.tasks["t2"] = {
            "parsed": {
                "data_rows": [{
                    "name": "TestUser",
                    "url": "",
                    "link_text": "TestDoc",
                    "existing_ai": "已过审",
                    "row_1indexed": 3,
                    "category": "南京大牌档",
                }],
                "col_map": {"AI审核": 5},
                "spreadsheet_token": "tok",
                "sheet_id": "sid",
            },
            "source": "feishu",
            "status": "parsed",
            "total": 1,
        }
        with patch.object(app_module, "_write_back_feishu", return_value=None), \
             patch("anthropic.Anthropic"):
            resp = client.get("/api/review/t2")
            events = _parse_sse(resp.data)
            item_events = [e for e in events if e["type"] == "item_done"]
            assert len(item_events) == 1
            assert item_events[0]["result"]["skipped"] is True
            done = [e for e in events if e["type"] == "done"][0]
            assert done["skipped"] == 1
            assert done["passed"] == 0

    def test_review_full_flow(self, client, mock_config):
        app_module.tasks["t3"] = {
            "parsed": {
                "data_rows": [{
                    "name": "Reviewer",
                    "url": "https://ai.feishu.cn/wiki/abc123",
                    "link_text": "TestDoc",
                    "existing_ai": "",
                    "row_1indexed": 3,
                    "category": "黑钻奔驰试驾",
                }],
                "col_map": {"AI审核": 5},
                "spreadsheet_token": "tok",
                "sheet_id": "sid",
            },
            "source": "feishu",
            "status": "parsed",
            "total": 1,
        }

        ai_response = json.dumps({
            "passed": True, "reason": "内容合规",
            "violations": [], "violation_quotes": []
        })

        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_anthropic_response(ai_response)

        with patch("anthropic.Anthropic", return_value=mock_client), \
             patch.object(app_module, "fetch_feishu_content", return_value=("文档内容", None)), \
             patch.object(app_module, "get_rules", return_value={"_common": "rules", "黑钻奔驰试驾": "special"}), \
             patch.object(app_module, "_write_back_feishu", return_value=None):
            resp = client.get("/api/review/t3")
            events = _parse_sse(resp.data)

            progress_events = [e for e in events if e["type"] == "progress"]
            assert len(progress_events) == 1
            assert progress_events[0]["account"] == "Reviewer"

            item_events = [e for e in events if e["type"] == "item_done"]
            assert item_events[0]["result"]["label"] == "已过审"

            done = [e for e in events if e["type"] == "done"][0]
            assert done["passed"] == 1
            assert done["failed"] == 0


class TestDownload:
    def test_nonexistent_task(self, client):
        resp = client.get("/api/download/nonexistent")
        assert resp.status_code == 404

    def test_no_result_path(self, client):
        app_module.tasks["t4"] = {"parsed": {}, "status": "done"}
        resp = client.get("/api/download/t4")
        assert resp.status_code == 404


# ─── 错误处理测试 ───

class TestFeishuRequest:
    def test_timeout(self, mock_feishu_token):
        import requests
        with patch.object(app_module.http_requests, "get", side_effect=requests.exceptions.Timeout):
            with pytest.raises(RuntimeError, match="超时"):
                feishu_request("get", "https://open.feishu.cn/test")

    def test_connection_error(self, mock_feishu_token):
        import requests
        with patch.object(app_module.http_requests, "get", side_effect=requests.exceptions.ConnectionError):
            with pytest.raises(RuntimeError, match="无法连接"):
                feishu_request("get", "https://open.feishu.cn/test")

    def test_http_error(self, mock_feishu_token):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = __import__("requests").exceptions.HTTPError(response=MagicMock(status_code=500))
        with patch.object(app_module.http_requests, "get", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="500"):
                feishu_request("get", "https://open.feishu.cn/test")

    def test_success(self, mock_feishu_token):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"code": 0, "data": "ok"}
        with patch.object(app_module.http_requests, "get", return_value=mock_resp):
            result = feishu_request("get", "https://open.feishu.cn/test")
            assert result["code"] == 0


class TestFetchFeishuContent:
    def test_bad_url(self, mock_feishu_token):
        content, err = app_module.fetch_feishu_content("https://google.com/something")
        assert content is None
        assert "无法解析" in err

    def test_no_token(self):
        with patch.object(app_module, "feishu_token", return_value=None):
            content, err = app_module.fetch_feishu_content("https://ai.feishu.cn/wiki/abc")
            assert content is None
            assert "token" in err


class TestGetRules:
    def test_returns_dict(self):
        """get_rules 应该返回 dict 结构"""
        with patch.object(app_module, "feishu_headers", return_value={"Authorization": "Bearer fake"}), \
             patch.object(app_module, "feishu_request") as mock_req:
            # Mock sheets query
            mock_req.side_effect = [
                {"data": {"sheets": [{"title": "执行大表", "sheet_id": "s1"}, {"title": "审核标准", "sheet_id": "s2"}]}},
                {"data": {"valueRange": {"values": [
                    ["品牌审核", "南京大牌档", "话题词要求..."],
                    [None, "奔驰试驾", "回搜词..."],
                    ["平台审核", "统一标准", None],
                    [None, None, "一、绝对化与极限词...这是通用规则"],
                ]}}}
            ]
            app_module._rules_cache.clear()
            result = get_rules("test_token_123")
            assert isinstance(result, dict)
            assert "_common" in result
            assert "南京大牌档" in result

    def test_fallback_to_pdf(self):
        """无飞书 token 时 fallback 到 PDF"""
        app_module._rules_cache.clear()
        with patch.object(app_module, "feishu_headers", return_value=None), \
             patch("os.path.exists", return_value=False):
            result = get_rules("any_token")
            assert isinstance(result, dict)
            assert result["_common"] == ""

    def test_cache(self):
        """第二次调用应使用缓存"""
        app_module._rules_cache["cached_token"] = {"_common": "cached rules"}
        result = get_rules("cached_token")
        assert result["_common"] == "cached rules"
        app_module._rules_cache.pop("cached_token", None)


class TestProcessRow:
    def test_passed_review(self):
        ai_resp = json.dumps({"passed": True, "reason": "ok", "violations": [], "violation_quotes": []})
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_anthropic_response(ai_resp)

        dr = {"name": "User1", "url": "", "link_text": "测试内容", "row_1indexed": 3, "category": "南京大牌档"}
        with patch.object(app_module, "fetch_feishu_content", return_value=(None, "no url")):
            result = _process_row(mock_client, "test-model", {"_common": "rules"}, dr)

        assert result["label"] == "已过审"
        assert result["skipped"] is False
        assert result["account"] == "User1"

    def test_failed_review(self):
        ai_resp = json.dumps({"passed": False, "reason": "违规", "violations": ["问题1"], "violation_quotes": ["原文1"]})
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_anthropic_response(ai_resp)

        dr = {"name": "User2", "url": "", "link_text": "问题内容", "row_1indexed": 4, "category": ""}
        result = _process_row(mock_client, "test-model", {"_common": ""}, dr)

        assert result["label"] == "未过审"
        assert len(result["violations"]) == 1
        assert len(result["violation_quotes"]) == 1

    def test_api_exception(self):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API error")

        dr = {"name": "User3", "url": "", "link_text": "content", "row_1indexed": 5, "category": ""}
        result = _process_row(mock_client, "m", {"_common": ""}, dr)

        assert result["label"] == "审核出错"
        assert "API error" in result["reason"]


class TestWriteBackFeishu:
    def test_writes_labels_and_notes(self):
        parsed = {"spreadsheet_token": "tok", "sheet_id": "sid"}
        col_map = {"AI审核": 9, "AI审核备注": 10}
        results = [
            {"row_1indexed": 3, "label": "已过审", "reason": "合规", "violations": [], "skipped": False, "url": "", "violation_quotes": []},
            {"row_1indexed": 4, "label": "未过审", "reason": "违规", "violations": ["问题1"], "skipped": False, "url": "", "violation_quotes": []},
        ]

        with patch.object(app_module, "write_feishu_sheet", return_value=None) as mock_write, \
             patch.object(app_module, "add_feishu_comment", return_value=None):
            err = _write_back_feishu(parsed, col_map, results)

        assert err is None
        assert mock_write.call_count == 2  # labels + notes

    def test_no_ai_col(self):
        parsed = {"spreadsheet_token": "tok", "sheet_id": "sid"}
        col_map = {}
        err = _write_back_feishu(parsed, col_map, [])
        assert "AI审核" in err

    def test_skips_skipped_results(self):
        parsed = {"spreadsheet_token": "tok", "sheet_id": "sid"}
        col_map = {"AI审核": 9}
        results = [{"row_1indexed": 3, "label": "已过审", "skipped": True, "url": ""}]

        with patch.object(app_module, "write_feishu_sheet", return_value=None) as mock_write:
            _write_back_feishu(parsed, col_map, results)

        mock_write.assert_not_called()


# ─── 项目配置管理测试 ───

class TestProjectConfigs:
    """测试CSV项目配置管理模块"""

    @pytest.fixture
    def sample_csv_content(self):
        """创建示例CSV内容"""
        return """项目名称,项目介绍,话题标签,利益点标准,口令要求,审核严格度
南京大牌档,南京特色餐厅,#美团黑钻会员 #南京大牌档,黑钻直升状元 绑定即领满100-50,南牌专属码,strict
跑腿1对1急送,急送服务,#美团黑钻会员 #跑腿急送,每月1次免费 提速20分钟,,normal
无畏契约,游戏活动,#美团会员瓦吗 #无畏契约,消费满400领限定刀皮,,normal
酒店权益,酒店优惠,#美团会员住酒店,最高85折 免费升房,,loose"""

    @pytest.fixture
    def sample_csv_file(self, sample_csv_content):
        """创建临时CSV文件"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as f:
            f.write(sample_csv_content)
            f.flush()
            yield f.name
        os.unlink(f.name)  # 清理临时文件

    @pytest.fixture
    def invalid_csv_file(self):
        """创建无效的CSV文件（缺失必需字段）"""
        content = """项目名称,项目介绍
南京大牌档,南京特色餐厅
跑腿急送,急送服务"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as f:
            f.write(content)
            f.flush()
            yield f.name
        os.unlink(f.name)

    def setup_method(self):
        """每个测试前清理缓存"""
        clear_project_configs_cache()

    def test_load_project_configs_success(self, sample_csv_file):
        """测试成功加载项目配置"""
        configs = load_project_configs(sample_csv_file)

        assert len(configs) == 4
        assert "南京大牌档" in configs
        assert "跑腿1对1急送" in configs

        # 检查配置内容
        njdpd_config = configs["南京大牌档"]
        assert njdpd_config["话题标签"] == "#美团黑钻会员 #南京大牌档"
        assert njdpd_config["利益点标准"] == "黑钻直升状元 绑定即领满100-50"
        assert njdpd_config["审核严格度"] == "strict"

    def test_load_project_configs_file_not_found(self):
        """测试文件不存在的情况"""
        with pytest.raises(FileNotFoundError, match="项目配置文件不存在"):
            load_project_configs("/nonexistent/path.csv")

    def test_load_project_configs_missing_required_fields(self, invalid_csv_file):
        """测试缺失必需字段的情况"""
        with pytest.raises(ValueError, match="CSV文件缺失必需字段"):
            load_project_configs(invalid_csv_file)

    def test_load_project_configs_empty_project_name(self):
        """测试空项目名称的处理"""
        content = """项目名称,话题标签,利益点标准
,#测试标签,测试利益点
有效项目,#有效标签,有效利益点"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as f:
            f.write(content)
            f.flush()

            configs = load_project_configs(f.name)

            # 应该跳过空名称的行，只加载有效项目
            assert len(configs) == 1
            assert "有效项目" in configs

        os.unlink(f.name)

    def test_load_project_configs_caching(self, sample_csv_file):
        """测试缓存机制"""
        # 第一次加载
        configs1 = load_project_configs(sample_csv_file)

        # 第二次加载应该使用缓存
        with patch('app.logger') as mock_logger:
            configs2 = load_project_configs(sample_csv_file)
            assert configs1 == configs2
            mock_logger.debug.assert_called_with(f"Using cached project configs for {sample_csv_file}")

    def test_get_project_config_exact_match(self, sample_csv_file):
        """测试精确匹配"""
        configs = load_project_configs(sample_csv_file)
        config = get_project_config("南京大牌档", configs)

        assert config is not None
        assert config["话题标签"] == "#美团黑钻会员 #南京大牌档"

    def test_get_project_config_partial_match(self, sample_csv_file):
        """测试包含匹配"""
        configs = load_project_configs(sample_csv_file)

        # 测试项目名称包含在配置key中的情况
        config = get_project_config("南京", configs)
        assert config is not None
        assert config["话题标签"] == "#美团黑钻会员 #南京大牌档"

    def test_get_project_config_reverse_partial_match(self, sample_csv_file):
        """测试反向包含匹配"""
        configs = load_project_configs(sample_csv_file)

        # 测试配置key包含在项目名称中的情况
        config = get_project_config("跑腿1对1急送服务", configs)
        assert config is not None
        assert config["话题标签"] == "#美团黑钻会员 #跑腿急送"

    def test_get_project_config_no_match(self, sample_csv_file):
        """测试未找到匹配的情况"""
        configs = load_project_configs(sample_csv_file)
        config = get_project_config("不存在的项目", configs)

        assert config is None

    def test_get_project_config_empty_name(self, sample_csv_file):
        """测试空项目名称"""
        configs = load_project_configs(sample_csv_file)

        assert get_project_config("", configs) is None
        assert get_project_config(None, configs) is None
        assert get_project_config("   ", configs) is None

    def test_get_project_config_auto_load(self):
        """测试自动加载配置"""
        # 模拟配置文件不存在的情况
        with patch('app.load_config') as mock_load_config:
            mock_load_config.return_value = {"project_config_path": "/nonexistent.csv"}
            config = get_project_config("测试项目")
            assert config is None

    def test_validate_project_config_valid(self):
        """测试有效配置验证"""
        valid_config = {
            "项目名称": "测试项目",
            "项目介绍": "这是一个测试项目",
            "话题标签": "#测试标签",
            "利益点标准": "测试利益点",
            "口令要求": "TEST123",
            "审核严格度": "strict"
        }

        result = validate_project_config(valid_config)
        assert result["项目名称"] == "测试项目"
        assert result["话题标签"] == "#测试标签"
        assert result["审核严格度"] == "strict"

    def test_validate_project_config_missing_required(self):
        """测试缺失必需字段"""
        invalid_config = {
            "项目名称": "测试项目",
            "项目介绍": "介绍"
            # 缺失话题标签和利益点标准
        }

        with pytest.raises(ValueError, match="话题标签不能为空"):
            validate_project_config(invalid_config)

    def test_validate_project_config_empty_required(self):
        """测试必需字段为空"""
        invalid_config = {
            "项目名称": "",
            "话题标签": "#标签",
            "利益点标准": "利益点"
        }

        with pytest.raises(ValueError, match="项目名称不能为空"):
            validate_project_config(invalid_config)

    def test_validate_project_config_invalid_audit_mode(self):
        """测试无效的审核严格度"""
        config = {
            "项目名称": "测试项目",
            "话题标签": "#标签",
            "利益点标准": "利益点",
            "审核严格度": "invalid_mode"
        }

        with patch('app.logger') as mock_logger:
            result = validate_project_config(config)
            assert result["审核严格度"] == "normal"  # 应该使用默认值
            mock_logger.warning.assert_called()

    def test_validate_project_config_missing_hashtag(self):
        """测试话题标签缺少#符号"""
        config = {
            "项目名称": "测试项目",
            "话题标签": "没有井号的标签",
            "利益点标准": "利益点",
            "审核严格度": "normal"  # 添加有效的审核严格度
        }

        with patch('app.logger') as mock_logger:
            result = validate_project_config(config)
            mock_logger.warning.assert_called_with("话题标签可能缺少#符号: 没有井号的标签")

    def test_validate_project_config_not_dict(self):
        """测试非字典类型的配置"""
        with pytest.raises(ValueError, match="配置必须是字典格式"):
            validate_project_config("not a dict")

    def test_clear_project_configs_cache(self):
        """测试清理缓存功能"""
        # 先加载一些配置到缓存
        app_module._project_configs_cache["test_key"] = {"test": "data"}

        # 清理缓存
        clear_project_configs_cache()

        # 验证缓存已清空
        assert len(app_module._project_configs_cache) == 0


# ─── LLM内容提取测试 ───

class TestLLMContentExtraction:
    """测试LLM内容元素提取模块"""

    @pytest.fixture
    def mock_anthropic_client(self):
        """Mock Anthropic客户端"""
        client = MagicMock()
        return client

    @pytest.fixture
    def sample_content(self):
        """示例稿件内容"""
        return """【美团黑钻会员专享】南京大牌档超值福利来啦！

#美团黑钻会员 #南京大牌档 #黑钻许愿真有用

黑钻会员直升「状元」身份，绑定即领满100-50进店见面礼！
还有免费小食、免排队特权（节假日不可用）。

快来体验正宗南京味道，用券码：NJDPD2024

@美团黑钻会员 让生活更有味道"""

    @pytest.fixture
    def valid_extraction_response(self):
        """有效的提取响应"""
        return json.dumps({
            "hashtags": ["#美团黑钻会员", "#南京大牌档", "#黑钻许愿真有用"],
            "slogans": ["NJDPD2024"],
            "benefits": ["黑钻会员直升「状元」身份", "绑定即领满100-50进店见面礼", "免费小食", "免排队特权"],
            "brands": ["美团黑钻会员", "南京大牌档"],
            "title": "美团黑钻会员专享南京大牌档超值福利来啦",
            "main_content": "黑钻会员直升「状元」身份，绑定即领满100-50进店见面礼！还有免费小食、免排队特权"
        })

    def test_extract_content_elements_success(self, mock_anthropic_client, sample_content, valid_extraction_response):
        """测试成功提取内容元素"""
        # Mock LLM响应
        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = valid_extraction_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = extract_content_elements(mock_anthropic_client, "claude-sonnet-4-6", sample_content)

        # 验证结果结构
        assert isinstance(result, dict)
        assert "hashtags" in result
        assert "slogans" in result
        assert "benefits" in result
        assert "brands" in result
        assert "title" in result
        assert "main_content" in result

        # 验证具体内容
        assert len(result["hashtags"]) == 3
        assert "#美团黑钻会员" in result["hashtags"]
        assert "NJDPD2024" in result["slogans"]
        assert len(result["benefits"]) == 4

        # 验证LLM调用参数
        mock_anthropic_client.messages.create.assert_called_once()
        call_args = mock_anthropic_client.messages.create.call_args
        assert call_args[1]["model"] == "claude-sonnet-4-6"
        assert call_args[1]["max_tokens"] == 2000
        assert sample_content in call_args[1]["messages"][0]["content"]

    def test_extract_content_elements_empty_content(self, mock_anthropic_client):
        """测试空内容处理"""
        result = extract_content_elements(mock_anthropic_client, "claude-sonnet-4-6", "")

        # 空内容应该返回默认结构，不调用LLM
        assert result == {
            "hashtags": [],
            "slogans": [],
            "benefits": [],
            "brands": [],
            "title": "",
            "main_content": ""
        }
        mock_anthropic_client.messages.create.assert_not_called()

    def test_extract_content_elements_whitespace_content(self, mock_anthropic_client):
        """测试只有空格的内容"""
        result = extract_content_elements(mock_anthropic_client, "claude-sonnet-4-6", "   \n\t   ")

        assert result == {
            "hashtags": [],
            "slogans": [],
            "benefits": [],
            "brands": [],
            "title": "",
            "main_content": ""
        }
        mock_anthropic_client.messages.create.assert_not_called()

    def test_extract_content_elements_json_parse_error(self, mock_anthropic_client, sample_content):
        """测试JSON解析错误处理"""
        # Mock无效JSON响应
        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = "这不是有效的JSON"
        mock_anthropic_client.messages.create.return_value = mock_message

        result = extract_content_elements(mock_anthropic_client, "claude-sonnet-4-6", sample_content)

        # 应该返回降级结果，main_content包含原内容
        assert result["hashtags"] == []
        assert result["slogans"] == []
        assert result["benefits"] == []
        assert result["brands"] == []
        assert result["title"] == ""
        assert result["main_content"] == sample_content

    def test_extract_content_elements_markdown_cleanup(self, mock_anthropic_client, sample_content, valid_extraction_response):
        """测试Markdown格式清理"""
        # Mock带markdown格式的响应
        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = f"```json\n{valid_extraction_response}\n```"
        mock_anthropic_client.messages.create.return_value = mock_message

        result = extract_content_elements(mock_anthropic_client, "claude-sonnet-4-6", sample_content)

        # 应该正确解析，忽略markdown格式
        assert len(result["hashtags"]) == 3
        assert "NJDPD2024" in result["slogans"]

    def test_extract_content_elements_missing_keys(self, mock_anthropic_client, sample_content):
        """测试响应缺少必需键的处理"""
        # Mock缺少部分键的响应
        incomplete_response = json.dumps({
            "hashtags": ["#标签1"],
            "benefits": ["利益点1"]
            # 缺少其他键
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = incomplete_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = extract_content_elements(mock_anthropic_client, "claude-sonnet-4-6", sample_content)

        # 缺少的键应该被设置为默认值
        assert result["hashtags"] == ["#标签1"]
        assert result["benefits"] == ["利益点1"]
        assert result["slogans"] == []  # 默认值
        assert result["brands"] == []   # 默认值
        assert result["title"] == ""    # 默认值
        assert result["main_content"] == ""  # 默认值

    def test_extract_content_elements_wrong_data_types(self, mock_anthropic_client, sample_content):
        """测试错误数据类型的处理"""
        # Mock错误数据类型的响应
        wrong_types_response = json.dumps({
            "hashtags": "#单个标签",  # 应该是数组
            "slogans": ["口令1"],
            "benefits": "单个利益点",  # 应该是数组
            "brands": ["品牌1"],
            "title": ["标题数组"],  # 应该是字符串
            "main_content": sample_content
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = wrong_types_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = extract_content_elements(mock_anthropic_client, "claude-sonnet-4-6", sample_content)

        # 数据类型应该被修正
        assert result["hashtags"] == ["#单个标签"]  # 转换为数组
        assert result["benefits"] == ["单个利益点"]  # 转换为数组
        assert result["title"] == "['标题数组']"    # 转换为字符串

    def test_extract_content_elements_api_exception(self, mock_anthropic_client, sample_content):
        """测试API调用异常处理"""
        # Mock API异常
        mock_anthropic_client.messages.create.side_effect = Exception("API调用失败")

        result = extract_content_elements(mock_anthropic_client, "claude-sonnet-4-6", sample_content)

        # 应该返回降级结果
        assert result["hashtags"] == []
        assert result["main_content"] == sample_content

    def test_extract_content_with_fallback_llm_success(self, mock_anthropic_client, sample_content, valid_extraction_response):
        """测试fallback函数LLM成功的情况"""
        # Mock成功的LLM响应
        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = valid_extraction_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = extract_content_with_fallback(mock_anthropic_client, "claude-sonnet-4-6", sample_content)

        # 应该使用LLM结果
        assert len(result["hashtags"]) == 3
        assert "NJDPD2024" in result["slogans"]

    def test_extract_content_with_fallback_regex_fallback(self, mock_anthropic_client):
        """测试fallback函数降级到正则的情况"""
        content_with_patterns = """这是测试内容 #测试标签 #另一个标签
        有优惠券代码 ABC123 和 DEF456
        享受5折优惠和免费配送权益
        某某品牌专属福利"""

        # 使用patch来模拟extract_content_elements抛出异常
        with patch('app.extract_content_elements') as mock_extract:
            mock_extract.side_effect = Exception("模拟LLM完全失败")

            result = extract_content_with_fallback(mock_anthropic_client, "claude-sonnet-4-6", content_with_patterns)

            # 应该使用正则提取结果
            assert "#测试标签" in result["hashtags"]
            assert "#另一个标签" in result["hashtags"]
            assert "ABC123" in result["slogans"] or "DEF456" in result["slogans"]
            assert any("折" in benefit for benefit in result["benefits"])
            assert result["main_content"] == content_with_patterns

    def test_extract_content_with_fallback_no_patterns(self, mock_anthropic_client):
        """测试fallback函数处理无模式内容"""
        # Mock LLM失败
        mock_anthropic_client.messages.create.side_effect = Exception("LLM失败")

        simple_content = "这是一段简单的文字，没有特殊模式"

        result = extract_content_with_fallback(mock_anthropic_client, "claude-sonnet-4-6", simple_content)

        # 正则提取应该返回空结果
        assert result["hashtags"] == []
        assert result["slogans"] == []
        assert result["benefits"] == []
        assert result["brands"] == []
        assert result["main_content"] == simple_content


# ─── LLM专项审核测试 ───

class TestLLMSpecificAudit:
    """测试LLM专项审核函数"""

    @pytest.fixture
    def mock_anthropic_client(self):
        """Mock Anthropic客户端"""
        client = MagicMock()
        return client

    # ─── 话题标签检查测试 ───

    def test_check_hashtags_llm_success(self, mock_anthropic_client):
        """测试话题标签检查成功"""
        # Mock成功的检查结果
        success_response = json.dumps({
            "passed": True,
            "missing": [],
            "reason": "所有必需标签都已包含",
            "found_tags": ["#美团黑钻会员", "#南京大牌档"]
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = success_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = check_hashtags_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["#美团黑钻会员", "#南京大牌档"],
            "#美团黑钻会员 #南京大牌档"
        )

        assert result["passed"] is True
        assert result["missing"] == []
        assert "标签" in result["reason"]
        assert len(result["found_tags"]) == 2

    def test_check_hashtags_llm_missing_tags(self, mock_anthropic_client):
        """测试话题标签缺失"""
        missing_response = json.dumps({
            "passed": False,
            "missing": ["#黑钻许愿真有用"],
            "reason": "缺失必需标签",
            "found_tags": ["#美团黑钻会员"]
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = missing_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = check_hashtags_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["#美团黑钻会员"],
            "#美团黑钻会员 #黑钻许愿真有用"
        )

        assert result["passed"] is False
        assert "#黑钻许愿真有用" in result["missing"]

    def test_check_hashtags_llm_no_requirements(self, mock_anthropic_client):
        """测试无标签要求的情况"""
        result = check_hashtags_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["#随便的标签"],
            ""
        )

        # 无要求时应该直接通过，不调用LLM
        assert result["passed"] is True
        assert "无标签要求" in result["reason"]
        mock_anthropic_client.messages.create.assert_not_called()

    def test_check_hashtags_llm_json_parse_error(self, mock_anthropic_client):
        """测试JSON解析错误"""
        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = "这不是JSON"
        mock_anthropic_client.messages.create.return_value = mock_message

        result = check_hashtags_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["#标签"],
            "#要求的标签"
        )

        assert result["passed"] is False
        assert "解析失败" in result["reason"]

    def test_check_hashtags_llm_api_exception(self, mock_anthropic_client):
        """测试API调用异常"""
        mock_anthropic_client.messages.create.side_effect = Exception("API失败")

        result = check_hashtags_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["#标签"],
            "#要求的标签"
        )

        assert result["passed"] is False
        assert "异常" in result["reason"]

    # ─── 利益点检查测试 ───

    def test_check_benefits_llm_success(self, mock_anthropic_client):
        """测试利益点检查成功"""
        success_response = json.dumps({
            "passed": True,
            "missing": [],
            "incorrect": [],
            "reason": "利益点描述准确",
            "matched_benefits": ["黑钻直升状元", "满100减50"]
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = success_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = check_benefits_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["黑钻直升状元身份", "绑定即领满100-50"],
            "黑钻直升状元 满100减50"
        )

        assert result["passed"] is True
        assert result["missing"] == []
        assert result["incorrect"] == []

    def test_check_benefits_llm_missing_benefits(self, mock_anthropic_client):
        """测试利益点缺失"""
        missing_response = json.dumps({
            "passed": False,
            "missing": ["免费升房"],
            "incorrect": [],
            "reason": "缺少关键利益点",
            "matched_benefits": ["黑钻状元"]
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = missing_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = check_benefits_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["黑钻状元身份"],
            "黑钻状元 免费升房"
        )

        assert result["passed"] is False
        assert "免费升房" in result["missing"]

    def test_check_benefits_llm_incorrect_benefits(self, mock_anthropic_client):
        """测试利益点错误"""
        incorrect_response = json.dumps({
            "passed": False,
            "missing": [],
            "incorrect": ["折扣描述不准确"],
            "reason": "利益点描述有误",
            "matched_benefits": []
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = incorrect_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = check_benefits_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["享受3折优惠"],
            "最高8.5折"
        )

        assert result["passed"] is False
        assert len(result["incorrect"]) > 0

    def test_check_benefits_llm_no_requirements(self, mock_anthropic_client):
        """测试无利益点要求"""
        result = check_benefits_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["任意利益点"],
            ""
        )

        assert result["passed"] is True
        assert "无利益点要求" in result["reason"]
        mock_anthropic_client.messages.create.assert_not_called()

    # ─── 口令检查测试 ───

    def test_check_slogans_llm_success(self, mock_anthropic_client):
        """测试口令检查成功"""
        success_response = json.dumps({
            "passed": True,
            "errors": [],
            "reason": "口令完全匹配",
            "found_slogans": ["NJDPD2024"],
            "expected_slogans": ["NJDPD2024"]
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = success_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = check_slogans_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["NJDPD2024"],
            "NJDPD2024"
        )

        assert result["passed"] is True
        assert result["errors"] == []

    def test_check_slogans_llm_incorrect_slogans(self, mock_anthropic_client):
        """测试口令错误"""
        error_response = json.dumps({
            "passed": False,
            "errors": ["口令拼写错误", "大小写不匹配"],
            "reason": "口令不匹配",
            "found_slogans": ["njdpd2024"],
            "expected_slogans": ["NJDPD2024"]
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = error_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = check_slogans_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["njdpd2024"],  # 小写错误
            "NJDPD2024"
        )

        assert result["passed"] is False
        assert len(result["errors"]) > 0

    def test_check_slogans_llm_no_requirements(self, mock_anthropic_client):
        """测试无口令要求"""
        result = check_slogans_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["任意口令"],
            ""
        )

        assert result["passed"] is True
        assert "无口令要求" in result["reason"]
        mock_anthropic_client.messages.create.assert_not_called()

    def test_check_slogans_llm_missing_structure(self, mock_anthropic_client):
        """测试响应结构缺失"""
        incomplete_response = json.dumps({
            "passed": False
            # 缺少其他字段
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = incomplete_response
        mock_anthropic_client.messages.create.return_value = mock_message

        result = check_slogans_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            ["TEST123"],
            "TEST123"
        )

        # 缺失字段应该被设置为默认值
        assert "errors" in result
        assert "reason" in result
        assert "found_slogans" in result
        assert "expected_slogans" in result

    # ─── Markdown格式清理测试 ───

    def test_markdown_cleanup_in_all_functions(self, mock_anthropic_client):
        """测试所有函数的Markdown格式清理"""
        markdown_response = "```json\n" + json.dumps({
            "passed": True,
            "missing": [],
            "reason": "测试Markdown清理"
        }) + "\n```"

        mock_message = MagicMock()
        mock_message.content = [MagicMock()]
        mock_message.content[0].text = markdown_response
        mock_anthropic_client.messages.create.return_value = mock_message

        # 测试话题标签函数
        result1 = check_hashtags_llm(mock_anthropic_client, "claude-sonnet-4-6", ["#标签"], "#标签")
        assert result1["passed"] is True

        # 测试利益点函数
        result2 = check_benefits_llm(mock_anthropic_client, "claude-sonnet-4-6", ["利益点"], "利益点")
        assert result2["passed"] is True

        # 测试口令函数
        result3 = check_slogans_llm(mock_anthropic_client, "claude-sonnet-4-6", ["CODE"], "CODE")
        assert result3["passed"] is True


# ─── 项目审核引擎测试 ───

class TestProjectAuditEngine:
    """测试项目审核引擎"""

    @pytest.fixture
    def mock_anthropic_client(self):
        """Mock Anthropic客户端"""
        return MagicMock()

    @pytest.fixture
    def sample_project_config(self):
        """示例项目配置"""
        return {
            "项目名称": "南京大牌档",
            "话题标签": "#美团黑钻会员 #南京大牌档",
            "利益点标准": "黑钻直升状元 绑定即领满100-50",
            "口令要求": "NJDPD2024",
            "审核严格度": "strict"
        }

    @pytest.fixture
    def sample_content(self):
        """示例稿件内容"""
        return """【美团黑钻会员专享】南京大牌档超值福利！

#美团黑钻会员 #南京大牌档

黑钻会员直升「状元」身份，绑定即领满100-50进店见面礼！
使用券码：NJDPD2024

快来体验正宗南京味道！"""

    @pytest.fixture
    def mock_successful_extraction(self):
        """Mock成功的内容提取结果"""
        return {
            "hashtags": ["#美团黑钻会员", "#南京大牌档"],
            "slogans": ["NJDPD2024"],
            "benefits": ["黑钻会员直升「状元」身份", "绑定即领满100-50进店见面礼"],
            "brands": ["美团黑钻会员", "南京大牌档"],
            "title": "美团黑钻会员专享南京大牌档超值福利",
            "main_content": "黑钻会员直升「状元」身份，绑定即领满100-50进店见面礼！使用券码：NJDPD2024 快来体验正宗南京味道！"
        }

    # ─── project_specific_review_llm 测试 ───

    def test_project_specific_review_llm_success(self, mock_anthropic_client, sample_project_config, sample_content, mock_successful_extraction):
        """测试项目专属审核成功"""
        # Mock内容提取和各项检查都成功
        with patch('app.extract_content_elements', return_value=mock_successful_extraction), \
             patch('app.check_hashtags_llm', return_value={"passed": True, "missing": [], "reason": "标签完整"}), \
             patch('app.check_benefits_llm', return_value={"passed": True, "missing": [], "incorrect": [], "reason": "利益点准确"}), \
             patch('app.check_slogans_llm', return_value={"passed": True, "errors": [], "reason": "口令正确"}):

            result = project_specific_review_llm(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                sample_content,
                sample_project_config
            )

            assert result["passed"] is True
            assert "通过" in result["reason"]
            assert result["content_elements"] == mock_successful_extraction
            assert result["hashtag_check"]["passed"] is True
            assert result["benefit_check"]["passed"] is True
            assert result["slogan_check"]["passed"] is True
            assert len(result["details"]) == 0

    def test_project_specific_review_llm_hashtag_failure(self, mock_anthropic_client, sample_project_config, sample_content, mock_successful_extraction):
        """测试话题标签检查失败"""
        with patch('app.extract_content_elements', return_value=mock_successful_extraction), \
             patch('app.check_hashtags_llm', return_value={"passed": False, "missing": ["#缺失标签"], "reason": "缺少必需标签"}), \
             patch('app.check_benefits_llm', return_value={"passed": True, "missing": [], "incorrect": [], "reason": "利益点准确"}), \
             patch('app.check_slogans_llm', return_value={"passed": True, "errors": [], "reason": "口令正确"}):

            result = project_specific_review_llm(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                sample_content,
                sample_project_config
            )

            assert result["passed"] is False
            assert "话题标签" in result["reason"]
            assert len(result["details"]) > 0
            assert any("话题标签问题" in detail for detail in result["details"])
            assert any("缺失标签" in detail for detail in result["details"])

    def test_project_specific_review_llm_benefit_failure(self, mock_anthropic_client, sample_project_config, sample_content, mock_successful_extraction):
        """测试利益点检查失败"""
        with patch('app.extract_content_elements', return_value=mock_successful_extraction), \
             patch('app.check_hashtags_llm', return_value={"passed": True, "missing": [], "reason": "标签完整"}), \
             patch('app.check_benefits_llm', return_value={"passed": False, "missing": ["免费升房"], "incorrect": ["折扣错误"], "reason": "利益点不准确"}), \
             patch('app.check_slogans_llm', return_value={"passed": True, "errors": [], "reason": "口令正确"}):

            result = project_specific_review_llm(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                sample_content,
                sample_project_config
            )

            assert result["passed"] is False
            assert "利益点" in result["reason"]
            assert any("利益点问题" in detail for detail in result["details"])
            assert any("缺失利益点" in detail for detail in result["details"])
            assert any("错误描述" in detail for detail in result["details"])

    def test_project_specific_review_llm_slogan_failure(self, mock_anthropic_client, sample_project_config, sample_content, mock_successful_extraction):
        """测试口令检查失败"""
        with patch('app.extract_content_elements', return_value=mock_successful_extraction), \
             patch('app.check_hashtags_llm', return_value={"passed": True, "missing": [], "reason": "标签完整"}), \
             patch('app.check_benefits_llm', return_value={"passed": True, "missing": [], "incorrect": [], "reason": "利益点准确"}), \
             patch('app.check_slogans_llm', return_value={"passed": False, "errors": ["口令拼写错误"], "reason": "口令不匹配"}):

            result = project_specific_review_llm(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                sample_content,
                sample_project_config
            )

            assert result["passed"] is False
            assert "口令" in result["reason"]
            assert any("口令问题" in detail for detail in result["details"])
            assert any("口令错误" in detail for detail in result["details"])

    def test_project_specific_review_llm_no_config(self, mock_anthropic_client, sample_content):
        """测试无项目配置的情况"""
        result = project_specific_review_llm(
            mock_anthropic_client,
            "claude-sonnet-4-6",
            sample_content,
            None
        )

        assert result["passed"] is True
        assert "无项目配置" in result["reason"]
        assert result["hashtag_check"]["passed"] is True
        assert result["benefit_check"]["passed"] is True
        assert result["slogan_check"]["passed"] is True

    def test_project_specific_review_llm_exception(self, mock_anthropic_client, sample_project_config, sample_content):
        """测试异常处理"""
        # Mock extract_content_elements抛出异常
        with patch('app.extract_content_elements', side_effect=Exception("提取失败")):
            result = project_specific_review_llm(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                sample_content,
                sample_project_config
            )

            assert result["passed"] is False
            assert "异常" in result["reason"]
            assert "提取失败" in result["reason"]

    # ─── enhanced_review_one 测试 ───

    def test_enhanced_review_one_all_pass(self, mock_anthropic_client, sample_project_config, sample_content):
        """测试项目审核和通用审核都通过"""
        mock_project_result = {
            "passed": True,
            "reason": "项目审核通过",
            "details": []
        }

        mock_general_result = {
            "passed": True,
            "reason": "通用审核通过",
            "violations": [],
            "violation_quotes": []
        }

        with patch('app.project_specific_review_llm', return_value=mock_project_result), \
             patch('app.review_one', return_value=mock_general_result):

            result = enhanced_review_one(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                sample_project_config,
                {"_common": "通用规则"},
                sample_content,
                "测试账号",
                "测试类型"
            )

            assert result["passed"] is True
            assert result["reason"] == "审核通过"
            assert result["violations"] == []
            assert result["project_review"] == mock_project_result
            assert result["general_review"] == mock_general_result

    def test_enhanced_review_one_project_fail(self, mock_anthropic_client, sample_project_config, sample_content):
        """测试项目审核失败，通用审核通过"""
        mock_project_result = {
            "passed": False,
            "reason": "项目审核失败",
            "details": ["缺少必需标签", "利益点不准确"]
        }

        mock_general_result = {
            "passed": True,
            "reason": "通用审核通过",
            "violations": [],
            "violation_quotes": []
        }

        with patch('app.project_specific_review_llm', return_value=mock_project_result), \
             patch('app.review_one', return_value=mock_general_result):

            result = enhanced_review_one(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                sample_project_config,
                {"_common": "通用规则"},
                sample_content,
                "测试账号",
                "测试类型"
            )

            assert result["passed"] is False
            assert "项目要求不符合" in result["reason"]
            assert len(result["violations"]) == 2
            assert "缺少必需标签" in result["violations"]
            assert "利益点不准确" in result["violations"]

    def test_enhanced_review_one_general_fail(self, mock_anthropic_client, sample_project_config, sample_content):
        """测试项目审核通过，通用审核失败"""
        mock_project_result = {
            "passed": True,
            "reason": "项目审核通过",
            "details": []
        }

        mock_general_result = {
            "passed": False,
            "reason": "通用审核失败",
            "violations": ["包含违禁词"],
            "violation_quotes": ["违禁内容"]
        }

        with patch('app.project_specific_review_llm', return_value=mock_project_result), \
             patch('app.review_one', return_value=mock_general_result):

            result = enhanced_review_one(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                sample_project_config,
                {"_common": "通用规则"},
                sample_content,
                "测试账号",
                "测试类型"
            )

            assert result["passed"] is False
            assert "内容违规" in result["reason"]
            assert "包含违禁词" in result["violations"]
            assert "违禁内容" in result["violation_quotes"]

    def test_enhanced_review_one_both_fail(self, mock_anthropic_client, sample_project_config, sample_content):
        """测试项目审核和通用审核都失败"""
        mock_project_result = {
            "passed": False,
            "reason": "项目审核失败",
            "details": ["标签缺失"]
        }

        mock_general_result = {
            "passed": False,
            "reason": "通用审核失败",
            "violations": ["违禁词"],
            "violation_quotes": ["不当内容"]
        }

        with patch('app.project_specific_review_llm', return_value=mock_project_result), \
             patch('app.review_one', return_value=mock_general_result):

            result = enhanced_review_one(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                sample_project_config,
                {"_common": "通用规则"},
                sample_content,
                "测试账号",
                "测试类型"
            )

            assert result["passed"] is False
            assert "项目要求不符合" in result["reason"]
            assert "内容违规" in result["reason"]
            assert len(result["violations"]) == 2  # 项目+通用
            assert len(result["violation_quotes"]) == 1

    def test_enhanced_review_one_no_project_config(self, mock_anthropic_client, sample_content):
        """测试无项目配置的情况"""
        mock_general_result = {
            "passed": True,
            "reason": "通用审核通过",
            "violations": [],
            "violation_quotes": []
        }

        with patch('app.review_one', return_value=mock_general_result):
            result = enhanced_review_one(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                None,  # 无项目配置
                {"_common": "通用规则"},
                sample_content,
                "测试账号",
                "测试类型"
            )

            assert result["passed"] is True
            assert result["project_review"] is None
            assert result["general_review"] == mock_general_result

    def test_enhanced_review_one_exception(self, mock_anthropic_client, sample_project_config, sample_content):
        """测试异常处理"""
        with patch('app.project_specific_review_llm', side_effect=Exception("系统异常")):
            result = enhanced_review_one(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                sample_project_config,
                {"_common": "通用规则"},
                sample_content,
                "测试账号",
                "测试类型"
            )

            assert result["passed"] is False
            assert "系统异常" in result["reason"]
            assert result["project_review"]["passed"] is False

    # ─── get_project_config_for_review 测试 ───

    def test_get_project_config_for_review_success(self):
        """测试成功获取项目配置"""
        mock_configs = {
            "南京大牌档": {"项目名称": "南京大牌档", "话题标签": "#标签"}
        }

        with patch('app.load_project_configs', return_value=mock_configs), \
             patch('app.get_project_config', return_value=mock_configs["南京大牌档"]) as mock_get:

            result = get_project_config_for_review("南京大牌档")

            assert result == mock_configs["南京大牌档"]
            mock_get.assert_called_once_with("南京大牌档", mock_configs)

    def test_get_project_config_for_review_not_found(self):
        """测试项目配置未找到"""
        with patch('app.load_project_configs', return_value={}), \
             patch('app.get_project_config', return_value=None):

            result = get_project_config_for_review("不存在的项目")

            assert result is None

    def test_get_project_config_for_review_exception(self):
        """测试获取配置时异常"""
        with patch('app.load_project_configs', side_effect=Exception("加载失败")):
            result = get_project_config_for_review("测试项目")

            assert result is None


# ─── 主审核流程集成测试 ───

class TestMainAuditFlowIntegration:
    """测试重构后的主审核流程集成"""

    @pytest.fixture
    def mock_anthropic_client(self):
        """Mock Anthropic客户端"""
        return MagicMock()

    @pytest.fixture
    def sample_project_config(self):
        """示例项目配置"""
        return {
            "项目名称": "南京大牌档",
            "话题标签": "#美团黑钻会员 #南京大牌档",
            "利益点标准": "黑钻直升状元 绑定即领满100-50",
            "口令要求": "NJDPD2024",
            "审核严格度": "strict"
        }

    @pytest.fixture
    def sample_dr(self):
        """示例数据行"""
        return {
            "name": "测试账号",
            "url": "https://example.feishu.cn/wiki/test",
            "link_text": "测试内容",
            "category": "南京大牌档",
            "row_1indexed": 2
        }

    def test_process_row_with_project_config_success(self, mock_anthropic_client, sample_project_config, sample_dr):
        """测试有项目配置的成功审核"""
        # Mock项目配置获取
        with patch('app.get_project_config_for_review', return_value=sample_project_config), \
             patch('app.fetch_feishu_content', return_value=("测试内容", None)), \
             patch('app.enhanced_review_one') as mock_enhanced_review:

            # Mock增强审核返回成功结果
            mock_enhanced_review.return_value = {
                "passed": True,
                "reason": "审核通过",
                "violations": [],
                "violation_quotes": [],
                "project_review": {"passed": True, "details": []},
                "general_review": {"passed": True}
            }

            result = _process_row(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                {"_common": "通用规则"},
                sample_dr
            )

            # 验证基本结果
            assert result["label"] == "已过审"
            assert result["account"] == "测试账号"
            assert result["has_project_config"] is True
            assert "匹配项目" in result["project_info"]

            # 验证调用了增强审核
            mock_enhanced_review.assert_called_once()
            call_args = mock_enhanced_review.call_args[0]  # 位置参数
            assert call_args[0] == mock_anthropic_client  # client
            assert call_args[1] == "claude-sonnet-4-6"    # model
            assert call_args[2] == sample_project_config  # project_config
            assert call_args[3] == {"_common": "通用规则"}  # rules_dict
            assert call_args[4] == "测试内容"              # content
            assert call_args[5] == "测试账号"              # name
            assert call_args[6] == "南京大牌档"            # category

    def test_process_row_with_project_config_failure(self, mock_anthropic_client, sample_project_config, sample_dr):
        """测试有项目配置的审核失败"""
        with patch('app.get_project_config_for_review', return_value=sample_project_config), \
             patch('app.fetch_feishu_content', return_value=("测试内容", None)), \
             patch('app.enhanced_review_one') as mock_enhanced_review:

            # Mock增强审核返回失败结果
            mock_enhanced_review.return_value = {
                "passed": False,
                "reason": "项目要求不符合; 内容违规",
                "violations": ["缺失必需标签", "包含违禁词"],
                "violation_quotes": ["违规内容"],
                "project_review": {
                    "passed": False,
                    "details": ["缺失必需标签: #黑钻许愿真有用", "利益点描述不准确"]
                },
                "general_review": {"passed": False, "violations": ["包含违禁词"]}
            }

            result = _process_row(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                {"_common": "通用规则"},
                sample_dr
            )

            # 验证失败结果
            assert result["label"] == "未过审"
            assert "项目要求" in result["detailed_reason"]
            assert len(result["violations"]) == 2
            assert result["project_review"]["passed"] is False

    def test_process_row_without_project_config(self, mock_anthropic_client, sample_dr):
        """测试无项目配置的审核"""
        # 修改dr去掉category
        dr_no_category = sample_dr.copy()
        dr_no_category["category"] = ""

        with patch('app.fetch_feishu_content', return_value=("测试内容", None)), \
             patch('app.enhanced_review_one') as mock_enhanced_review:

            # Mock增强审核（无项目配置）
            mock_enhanced_review.return_value = {
                "passed": True,
                "reason": "审核通过",
                "violations": [],
                "violation_quotes": [],
                "project_review": None,
                "general_review": {"passed": True}
            }

            result = _process_row(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                {"_common": "通用规则"},
                dr_no_category
            )

            # 验证无项目配置的结果
            assert result["label"] == "已过审"
            assert result["has_project_config"] is False
            assert result["project_info"] == "无项目匹配"
            assert result["project_review"] is None

            # 验证调用了增强审核，但project_config为None
            mock_enhanced_review.assert_called_once()
            call_args = mock_enhanced_review.call_args[0]  # 位置参数
            assert call_args[2] is None  # project_config是第3个位置参数

    def test_process_row_project_config_error(self, mock_anthropic_client, sample_dr):
        """测试项目配置获取失败"""
        with patch('app.get_project_config_for_review', side_effect=Exception("配置加载失败")), \
             patch('app.fetch_feishu_content', return_value=("测试内容", None)), \
             patch('app.enhanced_review_one') as mock_enhanced_review:

            mock_enhanced_review.return_value = {
                "passed": True,
                "reason": "审核通过",
                "violations": [],
                "violation_quotes": [],
                "project_review": None,
                "general_review": {"passed": True}
            }

            result = _process_row(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                {"_common": "通用规则"},
                sample_dr
            )

            # 验证配置获取失败时的处理
            assert result["has_project_config"] is False
            assert "配置获取失败" in result["project_info"]

    def test_process_row_enhanced_review_exception(self, mock_anthropic_client, sample_dr):
        """测试增强审核异常"""
        with patch('app.get_project_config_for_review', return_value=None), \
             patch('app.fetch_feishu_content', return_value=("测试内容", None)), \
             patch('app.enhanced_review_one', side_effect=Exception("增强审核失败")):

            result = _process_row(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                {"_common": "通用规则"},
                sample_dr
            )

            # 验证异常处理
            assert result["label"] == "审核出错"
            assert "增强审核异常" in result["reason"]
            assert "增强审核失败" in str(result["violations"])

    def test_write_back_feishu_enhanced_notes(self):
        """测试增强的飞书备注写入"""
        parsed = {"spreadsheet_token": "token123", "sheet_id": "sheet456"}
        col_map = {"AI审核": 5, "AI审核备注": 6}

        # 包含项目审核信息的结果
        results = [{
            "row_1indexed": 2,
            "label": "未过审",
            "reason": "项目要求不符合",
            "violations": ["缺失必需标签", "包含违禁词"],
            "project_info": "匹配项目: 南京大牌档",
            "project_review": {
                "passed": False,
                "details": ["缺失必需标签: #黑钻许愿真有用", "利益点描述不准确"]
            },
            "general_review": {"passed": False},
            "url": "https://example.feishu.cn/docs/test",
            "violation_quotes": ["违规内容"],
            "skipped": False
        }]

        with patch('app.write_feishu_sheet', return_value=None) as mock_write, \
             patch('app.add_feishu_comment', return_value=None) as mock_comment:

            error = _write_back_feishu(parsed, col_map, results)

            # 验证写入调用
            assert error is None
            assert mock_write.call_count == 2  # AI审核列 + 备注列

            # 验证备注内容
            notes_call = mock_write.call_args_list[1]
            notes_data = notes_call[0][3][0]  # 第4个位置参数rows_data的第一个元素
            notes_content = notes_data["label"]

            assert "项目要求不符合" in notes_content
            assert "📋 匹配项目: 南京大牌档" in notes_content
            assert "🔍 项目要求检查:" in notes_content
            assert "缺失必需标签" in notes_content
            assert "⚠️ 违规详情:" in notes_content

            # 验证添加了评论
            mock_comment.assert_called_once()

    def test_write_back_feishu_success_case(self):
        """测试成功审核的备注写入"""
        parsed = {"spreadsheet_token": "token123", "sheet_id": "sheet456"}
        col_map = {"AI审核": 5, "AI审核备注": 6}

        results = [{
            "row_1indexed": 2,
            "label": "已过审",
            "reason": "审核通过",
            "violations": [],
            "project_info": "匹配项目: 南京大牌档",
            "project_review": {"passed": True, "details": []},
            "skipped": False
        }]

        with patch('app.write_feishu_sheet', return_value=None) as mock_write, \
             patch('app.add_feishu_comment', return_value=None) as mock_comment:

            error = _write_back_feishu(parsed, col_map, results)

            assert error is None

            # 验证成功案例的备注内容
            notes_call = mock_write.call_args_list[1]
            notes_content = notes_call[0][3][0]["label"]  # 第4个位置参数rows_data的第一个元素的label

            assert "审核通过" in notes_content
            assert "📋 匹配项目: 南京大牌档" in notes_content
            # 成功案例不应该有项目要求检查和违规详情
            assert "🔍 项目要求检查:" not in notes_content
            assert "⚠️" not in notes_content

            # 成功案例不应该添加评论
            mock_comment.assert_not_called()

    def test_backward_compatibility(self, mock_anthropic_client):
        """测试向后兼容性"""
        # 使用旧格式的数据行（没有新字段）
        old_dr = {
            "name": "老用户",
            "url": "",
            "link_text": "老内容",
            "row_1indexed": 1
            # 没有category字段
        }

        with patch('app.fetch_feishu_content', return_value=(None, "no content")), \
             patch('app.enhanced_review_one') as mock_enhanced_review:

            # Mock原有格式的审核结果
            mock_enhanced_review.return_value = {
                "passed": True,
                "reason": "通过",
                "violations": [],
                "violation_quotes": [],
                "project_review": None,
                "general_review": {"passed": True}
            }

            result = _process_row(
                mock_anthropic_client,
                "claude-sonnet-4-6",
                {"_common": "规则"},
                old_dr
            )

            # 验证向后兼容
            assert "label" in result
            assert "reason" in result
            assert "violations" in result
            assert "account" in result
            # 新字段应该有默认值
            assert result["has_project_config"] is False
            assert result["project_info"] == "无项目匹配"


# ─── 主页测试 ───

class TestIndex:
    def test_index_loads(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "内容审核".encode("utf-8") in resp.data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
