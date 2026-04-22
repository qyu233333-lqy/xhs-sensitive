#!/usr/bin/env python3
"""core.feishu 单元测试"""

import os
import tempfile
import csv

from unittest.mock import patch, MagicMock

from core.review_engine import _benefit_requirement_matched
from core.review_engine import _is_already_reviewed, _create_existing_review_skip_result
from core.feishu import (
    _fetch_bitable_with_token,
    _fetch_sheets_data,
    add_feishu_comment,
    extract_review_doc_sections,
    _is_bitable_url,
    download_feishu_doc_snapshot,
    fetch_feishu_content,
    write_bitable_records,
)
from core.project import save_project_config
from core.project import load_project_configs_from_feishu


class TestIsBitableUrl:
    def test_sheets_url_with_table_query_is_not_bitable(self):
        url = "https://my.feishu.cn/sheets/AtX3sh4hphis1xtg1yzczpVbngf?table=tblOI4hCdkmyr1o5&sheet=OKkuCx"
        assert _is_bitable_url(url) is False

    def test_base_url_is_bitable(self):
        url = "https://my.feishu.cn/base/AtX3sh4hphis1xtg1yzczpVbngf?table=tblOI4hCdkmyr1o5"
        assert _is_bitable_url(url) is True


class TestFetchSheetsData:
    @patch("core.feishu._fetch_bitable_with_token")
    @patch("core.feishu.get_feishu_token", return_value="token-123")
    @patch("core.feishu.http_requests.get")
    def test_embedded_bitable_sheet_uses_bitable_api(self, mock_get, _mock_token, mock_fetch_bitable):
        meta_response = MagicMock()
        meta_response.status_code = 200
        meta_response.json.return_value = {
            "code": 0,
            "data": {
                "properties": {"title": "Test Sheet"},
                "sheets": [
                    {
                        "sheetId": "OKkuCx",
                        "title": "4月稿件审核表",
                        "blockInfo": {
                            "blockType": "BITABLE_BLOCK",
                            "blockToken": "CV5Ib9jICaY1i6sw3a3cFu8inEb_tblOI4hCdkmyr1o5",
                        },
                    }
                ],
            },
        }
        mock_get.return_value = meta_response
        mock_fetch_bitable.return_value = {
            "sheet_id": "tblOI4hCdkmyr1o5",
            "headers": [],
            "data": [],
            "title": "Untitled bitable",
            "total_rows": 0,
            "is_bitable": True,
        }

        result = _fetch_sheets_data(
            "https://my.feishu.cn/sheets/AtX3sh4hphis1xtg1yzczpVbngf?table=tblOI4hCdkmyr1o5&sheet=OKkuCx",
            "app-id",
            "app-secret",
        )

        assert result["is_bitable"] is True
        mock_fetch_bitable.assert_called_once_with(
            "CV5Ib9jICaY1i6sw3a3cFu8inEb",
            "tblOI4hCdkmyr1o5",
            "app-id",
            "app-secret",
            "https://my.feishu.cn/sheets/AtX3sh4hphis1xtg1yzczpVbngf?table=tblOI4hCdkmyr1o5&sheet=OKkuCx",
            auditable_only=True,
        )

    @patch("core.feishu.get_feishu_token", return_value="token-123")
    @patch("core.feishu.http_requests.get")
    def test_invalid_sheet_query_falls_back_to_first_sheet(self, mock_get, _mock_token):
        meta_response = MagicMock()
        meta_response.status_code = 200
        meta_response.json.return_value = {
            "code": 0,
            "data": {
                "properties": {"title": "Test Sheet"},
                "sheets": [
                    {"sheet_id": "sht_valid_1", "title": "Sheet1"},
                    {"sheet_id": "sht_valid_2", "title": "Sheet2"},
                ],
            },
        }

        data_response = MagicMock()
        data_response.status_code = 200
        data_response.json.return_value = {
            "code": 0,
            "data": {
                "valueRange": {
                    "values": [
                        ["昵称", "稿件链接"],
                        ["User1", "https://example.com/doc1"],
                    ]
                }
            },
        }

        mock_get.side_effect = [meta_response, data_response]

        result = _fetch_sheets_data(
            "https://my.feishu.cn/sheets/spreadsheet_token_123?sheet=OKkuCx&table=tbl123",
            "app-id",
            "app-secret",
        )

        assert result["sheet_id"] == "sht_valid_1"
        assert result["title"] == "Test Sheet"
        assert result["headers"] == ["昵称", "稿件链接"]
        assert result["total_rows"] == 1
        assert mock_get.call_args_list[1].args[0].endswith("/values/sht_valid_1!A1:ZZ2000")

    @patch("core.feishu.get_feishu_token", return_value="token-123")
    @patch("core.feishu.http_requests.get")
    def test_sheet_id_can_be_read_from_sheet_id_camel_case(self, mock_get, _mock_token):
        meta_response = MagicMock()
        meta_response.status_code = 200
        meta_response.json.return_value = {
            "code": 0,
            "data": {
                "properties": {"title": "Test Sheet"},
                "sheets": [
                    {"sheetId": "sht_camel_1", "title": "Sheet1"},
                ],
            },
        }

        data_response = MagicMock()
        data_response.status_code = 200
        data_response.json.return_value = {
            "code": 0,
            "data": {
                "valueRange": {
                    "values": [
                        ["昵称"],
                        ["User1"],
                    ]
                }
            },
        }

        mock_get.side_effect = [meta_response, data_response]

        result = _fetch_sheets_data(
            "https://my.feishu.cn/sheets/spreadsheet_token_123?sheet=invalid_from_url",
            "app-id",
            "app-secret",
        )

        assert result["sheet_id"] == "sht_camel_1"
        assert mock_get.call_args_list[1].args[0].endswith("/values/sht_camel_1!A1:ZZ2000")

    @patch("core.feishu.get_feishu_token", return_value="token-123")
    @patch("core.feishu.http_requests.get")
    def test_falls_back_to_sheet_title_when_sheet_id_range_fails(self, mock_get, _mock_token):
        meta_response = MagicMock()
        meta_response.status_code = 200
        meta_response.json.return_value = {
            "code": 0,
            "data": {
                "properties": {"title": "Test Sheet"},
                "sheets": [
                    {"sheet_id": "sht_valid_1", "title": "执行大表"},
                ],
            },
        }

        failed_data_response = MagicMock()
        failed_data_response.status_code = 200
        failed_data_response.json.return_value = {
            "code": 99991663,
            "msg": "not found sheetId",
        }

        success_data_response = MagicMock()
        success_data_response.status_code = 200
        success_data_response.json.return_value = {
            "code": 0,
            "data": {
                "valueRange": {
                    "values": [
                        ["昵称"],
                        ["User1"],
                    ]
                }
            },
        }

        mock_get.side_effect = [meta_response, failed_data_response, success_data_response]

        result = _fetch_sheets_data(
            "https://my.feishu.cn/sheets/spreadsheet_token_123?sheet=sht_valid_1",
            "app-id",
            "app-secret",
        )

        assert result["sheet_id"] == "sht_valid_1"
        assert mock_get.call_args_list[1].args[0].endswith("/values/sht_valid_1!A1:ZZ2000")
        assert mock_get.call_args_list[2].args[0].endswith("/values/执行大表!A1:ZZ2000")


class TestBitableApis:
    @patch("core.feishu.get_feishu_token", return_value="token-123")
    @patch("core.feishu.http_requests.get")
    def test_fetch_bitable_keeps_record_id(self, mock_get, _mock_token):
        app_response = MagicMock()
        app_response.status_code = 200
        app_response.json.return_value = {
            "code": 0,
            "data": {"app": {"name": "Untitled bitable"}},
        }

        records_response = MagicMock()
        records_response.status_code = 200
        records_response.json.return_value = {
            "code": 0,
            "data": {
                "items": [
                    {
                        "record_id": "rec123",
                        "fields": {
                            "fld1": "value1",
                            "稿件链接": {
                                "text": "示例稿件",
                                "link": "https://my.feishu.cn/wiki/JDUzwT1iliRNDfk5YsAckiTSnze",
                            },
                        },
                    }
                ]
            },
        }

        fields_response = MagicMock()
        fields_response.status_code = 200
        fields_response.json.return_value = {
            "code": 0,
            "data": {
                "items": [
                    {"field_id": "fld1", "field_name": "标题"},
                    {"field_id": "稿件链接", "field_name": "稿件链接"},
                ]
            },
        }

        mock_get.side_effect = [app_response, records_response, fields_response]

        result = _fetch_bitable_with_token("app_tok", "tbl123", "app-id", "app-secret", "https://example.com")

        assert result["app_token"] == "app_tok"
        assert result["data"][0]["_record_id"] == "rec123"
        assert result["data"][0]["标题"] == "value1"
        assert result["data"][0]["稿件链接"]["link"] == "https://my.feishu.cn/wiki/JDUzwT1iliRNDfk5YsAckiTSnze"

    @patch("core.feishu.get_feishu_token", return_value="token-123")
    @patch("core.feishu.http_requests.get")
    def test_fetch_bitable_can_keep_rows_without_auditable_link(self, mock_get, _mock_token):
        app_response = MagicMock()
        app_response.status_code = 200
        app_response.json.return_value = {
            "code": 0,
            "data": {"app": {"name": "Untitled bitable"}},
        }

        records_response = MagicMock()
        records_response.status_code = 200
        records_response.json.return_value = {
            "code": 0,
            "data": {"items": [{"record_id": "rec123", "fields": {"fld1": "规则A"}}]},
        }

        fields_response = MagicMock()
        fields_response.status_code = 200
        fields_response.json.return_value = {
            "code": 0,
            "data": {"items": [{"field_id": "fld1", "field_name": "项目名称"}]},
        }

        mock_get.side_effect = [app_response, records_response, fields_response]
        result = _fetch_bitable_with_token("app_tok", "tbl123", "app-id", "app-secret", "https://example.com", auditable_only=False)
        assert result["total_rows"] == 1

    @patch("core.feishu.get_feishu_token", return_value="token-123")
    @patch("core.feishu.http_requests.post")
    def test_write_bitable_records_uses_batch_update(self, mock_post, _mock_token):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"code": 0, "data": {}, "msg": "success"}
        mock_post.return_value = response

        ok = write_bitable_records(
            "app_tok",
            "tbl123",
            "app-id",
            "app-secret",
            [{"record_id": "rec123", "fields": {"AI审核状态（内部）": "审核通过"}}],
        )

        assert ok is True
        assert mock_post.call_args.args[0].endswith("/bitable/v1/apps/app_tok/tables/tbl123/records/batch_update")


class TestFeishuDocSnapshot:
    @patch("core.feishu.fetch_feishu_content", return_value="标题\n正文第一段")
    def test_download_feishu_doc_snapshot_writes_local_cache(self, _mock_fetch):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = download_feishu_doc_snapshot(
                "https://my.feishu.cn/wiki/JDUzwT1iliRNDfk5YsAckiTSnze",
                "app-id",
                "app-secret",
                output_dir=tmpdir,
            )

            assert result["url"] == "https://my.feishu.cn/wiki/JDUzwT1iliRNDfk5YsAckiTSnze"
            assert result["content"] == "标题\n正文第一段"
            assert result["local_path"].startswith(tmpdir)
            assert os.path.exists(result["local_path"])
            with open(result["local_path"], "r", encoding="utf-8") as f:
                assert f.read() == "标题\n正文第一段"


class TestFetchFeishuContent:
    @patch("core.feishu.get_feishu_token", return_value="token-123")
    @patch("core.feishu.http_requests.get")
    def test_wiki_uses_node_then_raw_content(self, mock_get, _mock_token):
        node_response = MagicMock()
        node_response.status_code = 200
        node_response.json.return_value = {
            "code": 0,
            "data": {
                "node": {
                    "title": "Wiki标题",
                    "obj_type": "docx",
                    "obj_token": "docx_real_token",
                }
            },
        }

        raw_content_response = MagicMock()
        raw_content_response.status_code = 200
        raw_content_response.json.return_value = {
            "code": 0,
            "data": {
                "content": "正文第一段\n正文第二段",
            },
        }

        mock_get.side_effect = [node_response, raw_content_response]

        content = fetch_feishu_content(
            "https://my.feishu.cn/wiki/XV2YwcSYwiUk9UkIPbKcc6onnqc",
            "app-id",
            "app-secret",
        )

        assert content == "Wiki标题\n正文第一段\n正文第二段"
        assert mock_get.call_args_list[0].args[0].endswith("/wiki/v2/nodes/XV2YwcSYwiUk9UkIPbKcc6onnqc")
        assert mock_get.call_args_list[1].args[0].endswith("/docx/v1/documents/docx_real_token/raw_content")

    @patch("core.feishu.get_feishu_token", return_value="token-123")
    @patch("core.feishu.http_requests.post")
    @patch("core.feishu.http_requests.get")
    def test_add_comment_resolves_wiki_to_docx_token(self, mock_get, mock_post, _mock_token):
        node_response = MagicMock()
        node_response.status_code = 200
        node_response.json.return_value = {
            "code": 0,
            "data": {"node": {"obj_token": "docx_real_token", "obj_type": "docx"}},
        }
        mock_get.return_value = node_response

        post_response = MagicMock()
        post_response.status_code = 200
        post_response.json.return_value = {"code": 0}
        mock_post.return_value = post_response

        ok = add_feishu_comment(
            "https://my.feishu.cn/wiki/XV2YwcSYwiUk9UkIPbKcc6onnqc",
            "app-id",
            "app-secret",
            "未过审原因：示例",
        )

        assert ok is True
        assert mock_post.call_args.args[0].endswith("/drive/v1/files/docx_real_token/comments?file_type=docx")


class TestExtractReviewDocSections:
    def test_extracts_title_body_and_comment(self):
        content = """
一、标题
我一直在 从来不是说说而已
四、发布文案
真正让人安心的，从来不是轰轰烈烈的海誓山盟
而是一句 我一直在
评论区置顶：上过春晚的乐队 One republic 演唱会这不得冲！
五、内容概述
这里是概述
        """.strip()

        result = extract_review_doc_sections(content)

        assert result["标题"] == "我一直在 从来不是说说而已"
        assert "真正让人安心的" in result["文案"]
        assert result["评论区文案"] == "上过春晚的乐队 One republic 演唱会这不得冲！"

    def test_extracts_title_when_header_is_numbered(self):
        content = """
相处技巧+钻哥的工具箱+ One republic演唱会+我一直在
三、标题
我一直在 从来不是说说而已 One republic演唱会也是
四、发布文案
真正让人安心的，从来不是轰轰烈烈的海誓山盟
而是一句 我一直在
评论区置顶：上过春晚的乐队One republic演唱会这不得冲！
五、内容概述
略
        """.strip()

        result = extract_review_doc_sections(content)

        assert result["标题"] == "我一直在 从来不是说说而已 One republic演唱会也是"
        assert "真正让人安心的" in result["文案"]


class TestBenefitRequirementMatched:
    def test_high_level_member_satisfies_baiyin_and_above(self):
        content = "美团黑钻会员，花超优惠的价格，享升级舒适体验"
        assert _benefit_requirement_matched(content, "白银及以上会员订酒店最高85折") is True

    def test_grade_increment_alias_is_accepted(self):
        content = "权益随等级递增，黑钻会员无限次享用核心权益"
        assert _benefit_requirement_matched(content, "按等级叠加解锁") is True

    def test_85_discount_alias_is_accepted(self):
        content = "订房最低85折，免费升房、免费早餐"
        assert _benefit_requirement_matched(content, "最高85折") is True


class TestReviewSkipLogic:
    def test_only_passed_row_is_detected(self):
        row = {"AI审核状态（内部）": "已过审"}
        assert _is_already_reviewed(row) is True

    def test_failed_row_is_not_skipped(self):
        row = {"AI审核状态（内部）": "未过审"}
        assert _is_already_reviewed(row) is False

    def test_existing_review_skip_result_marks_no_writeback(self):
        row = {"AI审核状态（内部）": "已过审"}
        result = _create_existing_review_skip_result(row)
        assert result["AI审核"] == "⏭️"
        assert result["_skip_writeback"] is True


class TestSaveProjectConfig:
    def test_create_or_update_project_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "ref.csv")
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["项目名称", "项目介绍", "话题标签", "利益点标准", "口令要求", "审核严格度"],
                )
                writer.writeheader()
                writer.writerow({
                    "项目名称": "旧项目",
                    "项目介绍": "",
                    "话题标签": "#旧标签",
                    "利益点标准": "旧标准",
                    "口令要求": "",
                    "审核严格度": "normal",
                })

            result = save_project_config({
                "项目名称": "新项目",
                "项目介绍": "描述",
                "话题标签": "#标签A #标签B",
                "利益点标准": "标准1\n标准2",
                "口令要求": "",
                "审核严格度": "normal",
            }, csv_path=csv_path)

            assert result["action"] == "created"

            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))

            assert any(row["项目名称"] == "新项目" for row in rows)


class TestLoadProjectConfigsFromFeishu:
    @patch("core.config.load_config")
    @patch("core.feishu.fetch_feishu_sheet")
    def test_loads_configs_from_feishu_rows(self, mock_fetch_sheet, mock_load_config):
        mock_load_config.return_value = {"feishu_app_id": "app-id", "feishu_app_secret": "app-secret"}
        mock_fetch_sheet.return_value = {
            "data": [{
                "项目名称": "One Republic演唱会",
                "项目介绍": "描述",
                "话题标签": "#演唱会 #黑钻许愿真有用",
                "利益点标准": "4.20 One republic-北京—22张门票",
                "审核严格度": "normal",
            }]
        }

        configs = load_project_configs_from_feishu("https://example.com/feishu")
        assert "One Republic演唱会" in configs
