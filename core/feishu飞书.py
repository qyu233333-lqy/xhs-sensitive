"""飞书API集成核心模块"""

import hashlib
import json
import os
import re
import time
import logging
import mimetypes
import xml.etree.ElementTree as ET
from typing import Dict, List, Any, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests as http_requests

logger = logging.getLogger(__name__)


class FeishuTokenManager:
    """飞书访问令牌管理器"""

    def __init__(self):
        self._token = None
        self._expires_at = 0

    def get_token(self, app_id: str, app_secret: str) -> Optional[str]:
        """获取有效的访问令牌，自动刷新过期令牌"""
        if self._token and time.time() < self._expires_at - 300:  # 提前5分钟刷新
            return self._token

        return self._refresh_token(app_id, app_secret)

    def _refresh_token(self, app_id: str, app_secret: str) -> Optional[str]:
        """刷新访问令牌"""
        try:
            response = http_requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 0:
                    self._token = data["tenant_access_token"]
                    self._expires_at = time.time() + data["expire"] - 300
                    logger.debug("Feishu token refreshed successfully")
                    return self._token
                else:
                    logger.error(f"Feishu token refresh failed: {data}")
            else:
                logger.error(f"Feishu API error: {response.status_code}")

        except Exception as e:
            logger.error(f"Failed to refresh Feishu token: {e}")

        return None


# 全局令牌管理器实例
_token_manager = FeishuTokenManager()


def get_feishu_token(app_id: str, app_secret: str) -> Optional[str]:
    """获取飞书访问令牌"""
    return _token_manager.get_token(app_id, app_secret)


def resolve_feishu_doc_url(value: Any) -> str:
    """从单元格值中尽量解析飞书文档 URL。

    支持:
    - 直接 URL
    - dict/list 里的 link/url/token/obj_token
    - 纯 token（按 docx token 处理）
    """
    def _from_scalar(text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""

        match = re.search(r'https?://[^\s]+', text)
        if match and any(part in match.group(0) for part in ["/docx/", "/wiki/"]):
            return match.group(0)

        if re.fullmatch(r'[A-Za-z0-9]{10,}', text):
            return f"https://my.feishu.cn/docx/{text}"

        return ""

    if value is None:
        return ""
    if isinstance(value, str):
        return _from_scalar(value)
    if isinstance(value, dict):
        for key in ("link", "url"):
            resolved = _from_scalar(str(value.get(key) or ""))
            if resolved:
                return resolved
        for key in ("token", "obj_token"):
            token = str(value.get(key) or "").strip()
            if token:
                return f"https://my.feishu.cn/docx/{token}"
        return _from_scalar(str(value.get("text") or ""))
    if isinstance(value, list):
        for item in value:
            resolved = resolve_feishu_doc_url(item)
            if resolved:
                return resolved
        return ""
    return ""


def split_feishu_doc_content(content: str) -> Dict[str, str]:
    """将飞书文档文本粗分为 标题/文案/评论区文案。"""
    lines = [line.strip() for line in (content or "").splitlines()]
    lines = [line for line in lines if line]

    if not lines:
        return {"标题": "", "文案": "", "评论区文案": ""}

    title = lines[0]
    body_lines = lines[1:]
    comment = ""

    def _is_comment_section_header(line: str) -> bool:
        normalized = re.sub(
            r"^(?:[一二三四五六七八九十1234567890ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[、.．\- ]*)?",
            "",
            line.replace("：", ":").strip(),
        )
        return bool(normalized) and "评论" in normalized

    for i, line in enumerate(body_lines):
        normalized = line.replace("：", ":")
        if _is_comment_section_header(line):
            comment = normalized.split(":", 1)[1].strip() if ":" in normalized else ""
            body_lines = body_lines[:i]
            break

    body = "\n".join(body_lines).strip()
    return {"标题": title, "文案": body, "评论区文案": comment}


def extract_review_doc_sections(content: str) -> Dict[str, str]:
    """从审核稿件文档中提取 标题/文案/评论区文案。"""
    text = (content or "").strip()
    if not text:
        return {"标题": "", "文案": "", "评论区文案": ""}
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    title_lines: List[str] = []
    body_lines: List[str] = []
    comment_lines: List[str] = []
    section = None

    def _extract_inline_value(line: str, pattern: str) -> Optional[str]:
        match = re.match(pattern, line)
        if not match:
            return None
        return (match.group(1) or "").strip()

    def _is_title_header(line: str) -> bool:
        return bool(re.match(r"^(?:[一二三四五六七八九十1234567890ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[、.．\- ]*)?标题\s*[：:]?$", line))

    def _is_body_header(line: str) -> bool:
        return bool(re.match(r"^(?:[一二三四五六七八九十1234567890ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[、.．\- ]*)?(?:发布文案|文案|正文|内容)\s*[：:]?$", line))

    def _strip_section_prefix(line: str) -> str:
        return re.sub(
            r"^(?:[一二三四五六七八九十1234567890ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[、.．\- ]*)?",
            "",
            line,
        ).strip()

    def _is_comment_header(line: str) -> bool:
        normalized = _strip_section_prefix(line.replace("：", ":"))
        return bool(normalized) and "评论" in normalized

    def _is_next_major_header(line: str) -> bool:
        return bool(re.match(r"^(?:[一二三四五六七八九十1234567890ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[、.．\- ]*)?(?:内容概述|参考视频|脚本内容|视频时长|视频尺寸)\s*[：:]?", line))

    def _looks_like_media_filename(line: str) -> bool:
        candidate = str(line or "").strip()
        if not candidate:
            return False
        return bool(
            re.match(
                r"^[^\s]+\.(?:jpg|jpeg|png|gif|webp|bmp|heic|mp4|mov|avi|m4v|wmv|mkv|webm)$",
                candidate,
                flags=re.IGNORECASE,
            )
        )

    for line in lines:
        inline_title = _extract_inline_value(
            line,
            r"^(?:[一二三四五六七八九十1234567890ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[、.．\- ]*)?标题\s*[：:]\s*(.+)$",
        )
        if inline_title is not None:
            section = "title"
            title_lines.append(inline_title)
            continue

        inline_body = _extract_inline_value(
            line,
            r"^(?:[一二三四五六七八九十1234567890ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[、.．\- ]*)?(?:发布文案|文案|正文|内容)\s*[：:]\s*(.+)$",
        )
        if inline_body is not None:
            section = "body"
            body_lines.append(inline_body)
            continue

        inline_comment = _extract_inline_value(
            line,
            r"^(?:[一二三四五六七八九十1234567890ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[、.．\- ]*)?[^：:\n]*评论[^：:\n]*\s*[：:]\s*(.+)$",
        )
        if inline_comment is not None:
            section = "comment"
            comment_lines.append(inline_comment)
            continue

        if _is_title_header(line):
            section = "title"
            continue
        if _is_body_header(line):
            section = "body"
            continue
        if _is_comment_header(line):
            normalized = re.sub(
                r"^(?:[一二三四五六七八九十1234567890ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[、.．\- ]*)?",
                "",
                line,
            ).strip()
            normalized = re.sub(r"^[^：:\n]*评论[^：:\n]*\s*[：:]?", "", normalized).strip()
            section = "comment"
            if normalized:
                comment_lines.append(normalized)
            continue
        if _is_next_major_header(line):
            section = None
            continue
        if _looks_like_media_filename(line):
            continue

        if section == "title":
            title_lines.append(line)
        elif section == "body":
            body_lines.append(line)
        elif section == "comment":
            comment_lines.append(line)

    title = "\n".join(title_lines).strip()
    body = "\n".join(body_lines).strip()
    comment = "\n".join(comment_lines).strip()

    if not title and not body and not comment:
        return split_feishu_doc_content(text)

    return {"标题": title, "文案": body, "评论区文案": comment}


def extract_feishu_ids(url: str) -> Tuple[Optional[str], Optional[str]]:
    """从飞书URL中提取spreadsheet_id和sheet_id

    Args:
        url: 飞书表格分享URL

    Returns:
        tuple: (spreadsheet_id, sheet_id) 或 (None, None) 如果解析失败
    """
    try:
        parsed = urlparse(url)

        # 从路径中提取spreadsheet_id
        path_parts = parsed.path.strip('/').split('/')
        spreadsheet_id = None

        for i, part in enumerate(path_parts):
            if ('spreadsheet' in part.lower() or 'sheets' in part.lower()) and i + 1 < len(path_parts):
                spreadsheet_id = path_parts[i + 1]
                break

        if not spreadsheet_id:
            logger.error(f"Could not extract spreadsheet_id from URL: {url}")
            return None, None

        # 从查询参数中提取sheet_id
        query_params = parse_qs(parsed.query)
        sheet_id = query_params.get('sheet', [None])[0]

        if not sheet_id:
            logger.warning(f"Could not extract sheet_id from URL, will use default: {url}")

        logger.debug(f"Extracted IDs: spreadsheet_id={spreadsheet_id}, sheet_id={sheet_id}")
        return spreadsheet_id, sheet_id

    except Exception as e:
        logger.error(f"Failed to extract Feishu IDs from URL {url}: {e}")
        return None, None


def fetch_feishu_sheet(url: str, app_id: str, app_secret: str, auditable_only: bool = True) -> Dict[str, Any]:
    """获取飞书表格数据，支持普通表格和Bitable（多维表格）

    Args:
        url: 飞书表格分享URL
        app_id: 飞书应用ID
        app_secret: 飞书应用密钥

    Returns:
        dict: 包含表格数据和元信息的字典
    """
    try:
        # wiki 链接需要先解析出其挂载的真实对象，再决定走 sheets 还是 bitable
        if "/wiki/" in url:
            token = get_feishu_token(app_id, app_secret)
            if not token:
                raise ValueError("无法获取飞书访问令牌")

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            node_info = _resolve_wiki_node_info(url, headers)
            obj_type = node_info.get("obj_type", "")
            obj_token = node_info.get("obj_token", "")
            wiki_title = node_info.get("title", "")
            if not obj_token:
                raise ValueError("无法从Wiki节点中解析对象ID")

            parsed = urlparse(url)
            query = parsed.query

            if obj_type in {"sheet", "spreadsheet"}:
                synthetic_url = f"https://my.feishu.cn/sheets/{obj_token}"
                if query:
                    synthetic_url = f"{synthetic_url}?{query}"
                logger.info(
                    "Resolved wiki sheet node: wiki_token=%s obj_token=%s title=%s",
                    parsed.path.rstrip("/").split("/")[-1],
                    obj_token,
                    wiki_title,
                )
                return _fetch_sheets_data(synthetic_url, app_id, app_secret, auditable_only=auditable_only)

            if obj_type in {"bitable", "base", "table"}:
                table_id = parse_qs(parsed.query).get("table", [None])[0]
                if not table_id:
                    raise ValueError("Wiki链接缺少table参数，无法解析多维表格")
                logger.info(
                    "Resolved wiki bitable node: wiki_token=%s app_token=%s table_id=%s title=%s",
                    parsed.path.rstrip("/").split("/")[-1],
                    obj_token,
                    table_id,
                    wiki_title,
                )
                return _fetch_bitable_with_token(obj_token, table_id, app_id, app_secret, url, auditable_only=auditable_only)

            raise ValueError(f"Wiki链接指向的对象类型不支持表格解析: {obj_type or 'unknown'}")

        # 检查是否是Bitable（多维表格）
        if _is_bitable_url(url):
            return _fetch_bitable_data(url, app_id, app_secret, auditable_only=auditable_only)
        else:
            return _fetch_sheets_data(url, app_id, app_secret, auditable_only=auditable_only)

    except Exception as e:
        logger.error(f"Failed to fetch Feishu sheet from {url}: {e}")
        raise


def _is_bitable_url(url: str) -> bool:
    """判断 URL 是否应按 Bitable（多维表格）处理。

    只把真正的 Bitable / Base 分享链接当成 Bitable。
    `sheets/...?...table=...` 这类链接仍应走普通 Sheets 解析。
    """
    parsed = urlparse(url)
    path = parsed.path.lower()

    if "/base/" in path or "/bitable/" in path:
        return True
    return False


def _resolve_wiki_node_info(url: str, headers: Dict[str, str]) -> Dict[str, str]:
    """解析 wiki 链接对应的节点信息。"""
    if '/wiki/' not in url:
        return {"obj_type": "", "obj_token": "", "title": ""}

    wiki_token = url.split('/wiki/')[1].split('?')[0].split('#')[0]
    node_api_url = f"https://open.feishu.cn/open-apis/wiki/v2/nodes/{wiki_token}"
    node_response = http_requests.get(node_api_url, headers=headers, timeout=15)

    node_result = None
    if node_response.status_code == 200:
        candidate = node_response.json()
        if candidate.get("code") == 0:
            node_result = candidate
    else:
        logger.warning("Wiki node API returned HTTP %s for %s, falling back to spaces/get_node", node_response.status_code, wiki_token)

    if node_result is None:
        fallback_api_url = "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node"
        fallback_response = http_requests.get(
            fallback_api_url,
            headers=headers,
            params={"token": wiki_token},
            timeout=15,
        )
        if fallback_response.status_code != 200:
            logger.error("Wiki fallback API error: HTTP %s", fallback_response.status_code)
            return {"obj_type": "", "obj_token": "", "title": ""}
        candidate = fallback_response.json()
        if candidate.get("code") != 0:
            logger.error("Wiki fallback API error: %s", candidate.get("msg", "Unknown error"))
            return {"obj_type": "", "obj_token": "", "title": ""}
        node_result = candidate

    node_data = node_result.get("data", {}).get("node", {})
    return {
        "obj_type": str(node_data.get("obj_type") or "").strip().lower(),
        "obj_token": str(node_data.get("obj_token") or "").strip(),
        "title": str(node_data.get("title") or "").strip(),
    }


def _extract_sheet_meta_id(sheet_meta: Dict[str, Any]) -> Optional[str]:
    """兼容飞书不同返回格式中的工作表 ID 字段名。"""
    for key in ("sheet_id", "sheetId", "id"):
        value = sheet_meta.get(key)
        if value:
            return str(value)
    return None


def _first_query_value(query_params: Dict[str, Any], key: str) -> Optional[str]:
    """从 parse_qs 的返回值中提取第一个标量值。"""
    value = query_params.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]
    value = str(value).strip()
    return value or None


def _cell_text(value: Any) -> str:
    """把飞书单元格值统一压成可做字典 key 的文本。"""
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or value.get("url") or "").strip()
    if isinstance(value, list):
        parts = [_cell_text(item) for item in value]
        return "; ".join([part for part in parts if part]).strip()
    return str(value).strip()


def _extract_sheet_meta_title(sheet_meta: Dict[str, Any]) -> Optional[str]:
    """兼容飞书不同返回格式中的工作表标题字段名。"""
    for key in ("title", "sheet_title", "sheetTitle", "name"):
        value = sheet_meta.get(key)
        if value:
            return str(value)
    return None


def _has_auditable_link(value: Any) -> bool:
    """是否存在可审核的稿件链接占位值。

    当前业务实际只需要保留“稿件链接”非空的行，即便字段内目前是纯文本标题而非 URL。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool((value.get("link") or value.get("url") or value.get("text") or "").strip())
    if isinstance(value, list):
        return any(_has_auditable_link(item) for item in value)
    return bool(str(value).strip())


def _extract_bitable_app_token_from_sheet_meta(sheet_meta: Dict[str, Any]) -> Optional[str]:
    """从 sheets metainfo 的 blockToken 中提取真正的 bitable app_token。"""
    block_info = sheet_meta.get("blockInfo") or {}
    block_token = block_info.get("blockToken")
    if not block_token or "_tbl" not in block_token:
        return None
    return block_token.split("_tbl", 1)[0]


def _fetch_sheet_values(
    spreadsheet_id: str,
    candidate_names: List[str],
    headers: Dict[str, str],
) -> Tuple[str, Dict[str, Any]]:
    """按多个候选工作表标识依次尝试读取数据。"""
    last_error = None

    for candidate in candidate_names:
        data_range = f"{candidate}!A1:ZZ2000"
        data_url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_id}/values/{data_range}"
        data_response = http_requests.get(data_url, headers=headers, timeout=20)

        if data_response.status_code != 200:
            last_error = f"获取表格数据失败: HTTP {data_response.status_code}"
            continue

        data_result = data_response.json()
        if data_result.get("code") == 0:
            return candidate, data_result

        last_error = f"获取表格数据失败: {data_result.get('msg', 'Unknown error')}"
        logger.warning("Failed to fetch sheet values with candidate %s: %s", candidate, last_error)

    raise ValueError(last_error or "获取表格数据失败: Unknown error")


def _fetch_bitable_with_token(app_token: str, table_id: str, app_id: str, app_secret: str, url: str,
                              auditable_only: bool = True) -> Dict[str, Any]:
    """使用明确的 app_token 和 table_id 获取 Bitable 数据。"""
    try:
        token = get_feishu_token(app_id, app_secret)
        if not token:
            raise ValueError("无法获取飞书访问令牌")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        app_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
        app_response = http_requests.get(app_url, headers=headers, timeout=15)
        if app_response.status_code != 200:
            raise ValueError(f"获取Bitable应用信息失败: HTTP {app_response.status_code}")

        app_data = app_response.json()
        if app_data.get("code") != 0:
            raise ValueError(f"获取Bitable应用信息失败: {app_data.get('msg', 'Unknown error')}")

        app_info = app_data["data"]["app"]

        records_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        records_response = http_requests.get(records_url, headers=headers, params={"page_size": 500}, timeout=20)
        if records_response.status_code != 200:
            raise ValueError(f"获取Bitable记录失败: HTTP {records_response.status_code}")

        records_data = records_response.json()
        if records_data.get("code") != 0:
            raise ValueError(f"获取Bitable记录失败: {records_data.get('msg', 'Unknown error')}")

        fields_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
        fields_response = http_requests.get(fields_url, headers=headers, timeout=15)
        if fields_response.status_code != 200:
            raise ValueError(f"获取Bitable字段信息失败: HTTP {fields_response.status_code}")

        fields_data = fields_response.json()
        if fields_data.get("code") != 0:
            raise ValueError(f"获取Bitable字段信息失败: {fields_data.get('msg', 'Unknown error')}")

        field_map = {}
        headers_row = []
        for field in fields_data["data"]["items"]:
            field_id = str(field.get("field_id") or "").strip()
            field_name = str(field.get("field_name") or "").strip()
            if not field_id:
                continue
            field_map[field_id] = field_name
            headers_row.append(field_name)

        data_rows = []
        for i, record in enumerate(records_data["data"]["items"], 1):
            row_dict = {"_row_index": i + 1}
            row_dict["_record_id"] = record.get("record_id") or record.get("id")
            fields = record.get("fields", {})

            for field_id, value in fields.items():
                field_name = field_map.get(field_id, field_id)

                if field_name == "稿件链接":
                    row_dict[field_name] = value
                    continue

                if isinstance(value, dict):
                    if "text" in value:
                        row_dict[field_name] = value["text"]
                    elif "link" in value:
                        row_dict[field_name] = value["link"]
                    else:
                        row_dict[field_name] = str(value)
                elif isinstance(value, list) and value:
                    if isinstance(value[0], dict) and "text" in value[0]:
                        row_dict[field_name] = "; ".join([item["text"] for item in value])
                    else:
                        row_dict[field_name] = "; ".join([str(item) for item in value])
                else:
                    row_dict[field_name] = str(value) if value is not None else ""

            for header in headers_row:
                if header not in row_dict:
                    row_dict[header] = ""

            if (not auditable_only) or _has_auditable_link(fields.get("稿件链接")):
                data_rows.append(row_dict)

        result = {
            "spreadsheet_id": app_token,
            "app_token": app_token,
            "sheet_id": table_id,
            "title": app_info.get("name", "Unknown Bitable"),
            "headers": headers_row,
            "data": data_rows,
            "total_rows": len(data_rows),
            "url": url,
            "is_bitable": True
        }
        logger.info(f"Successfully fetched Bitable data: {len(data_rows)} rows, {len(headers_row)} columns")
        return result

    except Exception as e:
        logger.error(f"Failed to fetch Bitable data: {e}")
        raise


def _fetch_bitable_data(url: str, app_id: str, app_secret: str, auditable_only: bool = True) -> Dict[str, Any]:
    """获取 Bitable（多维表格）数据。"""
    parsed = urlparse(url)
    path_parts = parsed.path.strip('/').split('/')
    query_params = parse_qs(parsed.query)

    app_token = None
    for i, part in enumerate(path_parts):
        if ('base' in part.lower() or 'bitable' in part.lower() or 'sheets' in part.lower()) and i + 1 < len(path_parts):
            app_token = path_parts[i + 1]
            break

    table_id = _first_query_value(query_params, "table")

    if not app_token:
        raise ValueError("无法从URL中提取app_token")
    if not table_id:
        raise ValueError("无法从URL中提取table_id")

    logger.info(f"Detected Bitable: app_token={app_token}, table_id={table_id}")
    return _fetch_bitable_with_token(app_token, table_id, app_id, app_secret, url, auditable_only=auditable_only)


def _fetch_sheets_data(url: str, app_id: str, app_secret: str, auditable_only: bool = True) -> Dict[str, Any]:
    """获取普通电子表格数据"""
    try:
        # 提取表格和工作表ID
        spreadsheet_id, sheet_id = extract_feishu_ids(url)
        if not spreadsheet_id:
            raise ValueError("无法从URL中提取表格ID")

        # 获取访问令牌
        token = get_feishu_token(app_id, app_secret)
        if not token:
            raise ValueError("无法获取飞书访问令牌")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # 获取表格元信息
        meta_url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_id}/metainfo"
        meta_response = http_requests.get(meta_url, headers=headers, timeout=15)

        if meta_response.status_code != 200:
            raise ValueError(f"获取表格元信息失败: HTTP {meta_response.status_code}")

        meta_data = meta_response.json()
        if meta_data.get("code") != 0:
            raise ValueError(f"获取表格元信息失败: {meta_data.get('msg', 'Unknown error')}")

        sheets = meta_data["data"]["sheets"]
        query_params = parse_qs(urlparse(url).query)
        requested_table_id = _first_query_value(query_params, "table")

        # 对于嵌入在 Sheets 中的 Bitable block，真实数据必须走 Bitable API。
        if requested_table_id:
            for sheet in sheets:
                block_info = sheet.get("blockInfo") or {}
                block_token = block_info.get("blockToken", "")
                if block_info.get("blockType") == "BITABLE_BLOCK" and requested_table_id in block_token:
                    app_token = _extract_bitable_app_token_from_sheet_meta(sheet)
                    if not app_token:
                        raise ValueError("无法从工作表元信息中提取 Bitable app_token")
                    logger.info(
                        "Detected embedded Bitable block in spreadsheet %s: app_token=%s, table_id=%s",
                        spreadsheet_id,
                        app_token,
                        requested_table_id
                    )
                    return _fetch_bitable_with_token(
                        app_token,
                        requested_table_id,
                        app_id,
                        app_secret,
                        url,
                        auditable_only=auditable_only,
                    )

        # 如果没有指定sheet_id，使用第一个工作表
        valid_sheet_ids = [_extract_sheet_meta_id(sheet) for sheet in sheets]
        valid_sheet_ids = [sheet_id_value for sheet_id_value in valid_sheet_ids if sheet_id_value]
        valid_sheet_titles = [_extract_sheet_meta_title(sheet) for sheet in sheets]
        valid_sheet_titles = [sheet_title for sheet_title in valid_sheet_titles if sheet_title]

        if not sheet_id and sheets:
            sheet_id = valid_sheet_ids[0] if valid_sheet_ids else None
            logger.info(f"Using default sheet: {sheet_id}")
        elif sheet_id and sheet_id not in valid_sheet_ids and sheets:
            original_sheet_id = sheet_id
            sheet_id = valid_sheet_ids[0] if valid_sheet_ids else None
            logger.warning(
                "Sheet ID from URL is not valid for spreadsheet %s, fallback from %s to %s",
                spreadsheet_id,
                original_sheet_id,
                sheet_id
            )

        if not sheet_id:
            sheet_keys = [sorted(sheet.keys()) for sheet in sheets[:3]]
            raise ValueError(f"无法确定工作表ID，metainfo sheets keys={sheet_keys}")

        value_candidates = []
        for candidate in [sheet_id, *valid_sheet_ids, *valid_sheet_titles]:
            if candidate and candidate not in value_candidates:
                value_candidates.append(candidate)

        used_sheet_range, data_result = _fetch_sheet_values(spreadsheet_id, value_candidates, headers)
        logger.info("Fetched sheet values using range prefix: %s", used_sheet_range)

        # 处理表格数据
        raw_values = data_result["data"]["valueRange"]["values"]
        if not raw_values:
            raise ValueError("表格数据为空")

        # 转换为字典格式
        headers_row = []
        for idx, header in enumerate(raw_values[0]):
            header_text = _cell_text(header)
            headers_row.append(header_text or f"列{idx + 1}")
        data_rows = []

        for i, row in enumerate(raw_values[1:], 1):
            row_dict = {"_row_index": i + 1}  # 1-based行号

            for j, header in enumerate(headers_row):
                header_key = header or f"列{j + 1}"
                if j < len(row):
                    cell_data = row[j]
                    if isinstance(cell_data, dict):
                        # 处理富文本或链接
                        if "text" in cell_data:
                            row_dict[header_key] = cell_data["text"]
                        elif "link" in cell_data:
                            row_dict[header_key] = cell_data["link"]
                        else:
                            row_dict[header_key] = str(cell_data)
                    else:
                        row_dict[header_key] = _cell_text(cell_data)
                else:
                    row_dict[header_key] = ""

            data_rows.append(row_dict)

        result = {
            "spreadsheet_id": spreadsheet_id,
            "sheet_id": sheet_id,
            "title": meta_data["data"]["properties"]["title"],
            "headers": headers_row,
            "data": data_rows,
            "total_rows": len(data_rows),
            "url": url
        }

        logger.info(f"Successfully fetched Feishu sheet: {len(data_rows)} rows")
        return result

    except Exception as e:
        logger.error(f"Failed to fetch Feishu sheet from {url}: {e}")
        raise


def write_feishu_sheet(spreadsheet_id: str, sheet_id: str, app_id: str, app_secret: str,
                      updates: List[Dict[str, Any]]) -> bool:
    """批量写入飞书表格数据

    Args:
        spreadsheet_id: 表格ID
        sheet_id: 工作表ID
        app_id: 飞书应用ID
        app_secret: 飞书应用密钥
        updates: 更新数据列表，格式：[{"range": "A1", "values": [["value1", "value2"]]}]

    Returns:
        bool: 写入是否成功
    """
    try:
        token = get_feishu_token(app_id, app_secret)
        if not token:
            logger.error("Failed to get Feishu token for writing")
            return False

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # 构建批量更新请求
        requests_data = []
        for update in updates:
            requests_data.append({
                "range": f"{sheet_id}!{update['range']}",
                "values": update["values"]
            })

        update_url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_id}/values_batch_update"
        payload = {
            "valueInputOption": "USER_ENTERED",
            "data": requests_data
        }

        response = http_requests.post(update_url, headers=headers, json=payload, timeout=20)

        if response.status_code == 200:
            result = response.json()
            if result.get("code") == 0:
                logger.info(f"Successfully wrote {len(updates)} updates to Feishu sheet")
                return True
            else:
                logger.error(f"Feishu write failed: {result.get('msg', 'Unknown error')}")
        else:
            logger.error(f"Feishu write HTTP error: {response.status_code}")

        return False

    except Exception as e:
        logger.error(f"Failed to write to Feishu sheet: {e}")
        return False


def write_bitable_records(app_token: str, table_id: str, app_id: str, app_secret: str,
                          updates: List[Dict[str, Any]]) -> bool:
    """批量写入飞书多维表格记录。

    updates 格式:
    [{"record_id": "recxxxx", "fields": {"AI审核状态（内部）": "审核通过"}}]
    """
    try:
        token = get_feishu_token(app_id, app_secret)
        if not token:
            logger.error("Failed to get Feishu token for bitable writing")
            return False

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        update_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"

        for i in range(0, len(updates), 500):
            batch = updates[i:i + 500]
            payload = {"records": batch}
            response = http_requests.post(update_url, headers=headers, json=payload, timeout=20)

            if response.status_code != 200:
                logger.error(f"Feishu bitable write HTTP error: {response.status_code}")
                return False

            result = response.json()
            if result.get("code") != 0:
                logger.error(f"Feishu bitable write failed: {result.get('msg', 'Unknown error')}")
                return False

        logger.info(f"Successfully wrote {len(updates)} updates to Feishu bitable")
        return True

    except Exception as e:
        logger.error(f"Failed to write to Feishu bitable: {e}")
        return False


def create_bitable_attachment_fields(
    app_token: str,
    table_id: str,
    app_id: str,
    app_secret: str,
    field_names: List[str],
) -> Dict[str, Any]:
    """为多维表格批量补建附件字段。"""
    try:
        token = get_feishu_token(app_id, app_secret)
        if not token:
            return {"ok": False, "error": "获取飞书访问令牌失败", "created": []}

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        create_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
        created: List[str] = []

        for field_name in field_names:
            response = http_requests.post(
                create_url,
                headers=headers,
                json={
                    "field_name": field_name,
                    "type": 17,  # 附件
                    "property": {},
                },
                timeout=20,
            )

            if response.status_code != 200:
                return {"ok": False, "error": f"创建字段 {field_name} 失败: HTTP {response.status_code}", "created": created}

            result = response.json()
            if result.get("code") != 0:
                return {
                    "ok": False,
                    "error": f"创建字段 {field_name} 失败: {result.get('msg', 'Unknown error')}",
                    "created": created,
                }

            created.append(field_name)

        if created:
            logger.info(
                "Successfully created %s bitable attachment fields: %s",
                len(created),
                ", ".join(created),
            )
        return {"ok": True, "created": created}
    except Exception as e:
        logger.error("Failed to create bitable attachment fields: %s", e)
        return {"ok": False, "error": str(e), "created": []}


def add_feishu_comment(doc_url: str, app_id: str, app_secret: str, comment: str) -> bool:
    """为飞书文档添加评论

    Args:
        doc_url: 飞书文档URL
        app_id: 飞书应用ID
        app_secret: 飞书应用密钥
        comment: 评论内容

    Returns:
        bool: 添加评论是否成功
    """
    try:
        token = get_feishu_token(app_id, app_secret)
        if not token:
            logger.error("Failed to get Feishu token for commenting")
            return False

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        doc_id = _resolve_docx_token_from_url(doc_url, headers)
        if not doc_id:
            logger.error(f"Could not resolve commentable docx token from URL: {doc_url}")
            return False

        comment_url = f"https://open.feishu.cn/open-apis/drive/v1/files/{doc_id}/comments?file_type=docx"
        payload = {
            "reply_list": {
                "replies": [
                    {
                        "content": {
                            "elements": [
                                {
                                    "type": "text_run",
                                    "text_run": {
                                        "text": comment
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        }

        response = http_requests.post(comment_url, headers=headers, json=payload, timeout=15)

        if response.status_code == 200:
            result = response.json()
            if result.get("code") == 0:
                logger.info(f"Successfully added comment to document {doc_id}")
                return True
            else:
                logger.error(f"Feishu comment failed: {result.get('msg', 'Unknown error')}")
        else:
            logger.error(f"Feishu comment HTTP error: {response.status_code}")

        return False

    except Exception as e:
        logger.error(f"Failed to add comment to Feishu document {doc_url}: {e}")
        return False


def _resolve_docx_token_from_url(url: str, headers: Dict[str, str]) -> str:
    """将 docx/wiki URL 解析为真正可用于 docx API 的 token。"""
    if '/docx/' in url:
        return url.split('/docx/')[1].split('?')[0].split('#')[0]

    if '/wiki/' not in url:
        return ""

    wiki_token = url.split('/wiki/')[1].split('?')[0].split('#')[0]
    node_api_url = f"https://open.feishu.cn/open-apis/wiki/v2/nodes/{wiki_token}"
    node_response = http_requests.get(node_api_url, headers=headers, timeout=15)

    node_result = None
    if node_response.status_code == 200:
        candidate = node_response.json()
        if candidate.get("code") == 0:
            node_result = candidate
    else:
        logger.warning("Wiki node API returned HTTP %s for %s, falling back to spaces/get_node", node_response.status_code, wiki_token)

    if node_result is None:
        fallback_api_url = "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node"
        fallback_response = http_requests.get(
            fallback_api_url,
            headers=headers,
            params={"token": wiki_token},
            timeout=15,
        )
        if fallback_response.status_code != 200:
            logger.error("Wiki fallback API error: HTTP %s", fallback_response.status_code)
            return ""
        candidate = fallback_response.json()
        if candidate.get("code") != 0:
            logger.error("Wiki fallback API error: %s", candidate.get("msg", "Unknown error"))
            return ""
        node_result = candidate

    node_data = node_result.get("data", {}).get("node", {})
    return str(node_data.get("obj_token") or "").strip()


def fetch_feishu_content(url: str, app_id: str, app_secret: str) -> str:
    """获取飞书文档内容

    Args:
        url: 飞书文档URL
        app_id: 飞书应用ID
        app_secret: 飞书应用密钥

    Returns:
        str: 文档文本内容
    """
    try:
        # 判断文档类型并提取ID
        doc_id = None
        is_wiki = False

        if '/docx/' in url:
            doc_id = url.split('/docx/')[1].split('?')[0].split('#')[0]
        elif '/wiki/' in url:
            doc_id = url.split('/wiki/')[1].split('?')[0].split('#')[0]
            is_wiki = True

        if not doc_id:
            logger.error(f"Could not extract document ID from URL: {url}")
            return ""

        token = get_feishu_token(app_id, app_secret)
        if not token:
            logger.error("Failed to get Feishu token for content fetching")
            return ""

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        docx_token = doc_id
        content = ""
        wiki_title = ""

        if is_wiki:
            node_result = None

            node_api_url = f"https://open.feishu.cn/open-apis/wiki/v2/nodes/{doc_id}"
            node_response = http_requests.get(node_api_url, headers=headers, timeout=15)

            if node_response.status_code == 200:
                candidate = node_response.json()
                if candidate.get("code") == 0:
                    node_result = candidate
            else:
                logger.warning(f"Wiki node API returned HTTP {node_response.status_code}, falling back to spaces/get_node")

            if node_result is None:
                fallback_api_url = "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node"
                fallback_response = http_requests.get(
                    fallback_api_url,
                    headers=headers,
                    params={"token": doc_id},
                    timeout=15,
                )

                if fallback_response.status_code != 200:
                    logger.error(f"Wiki fallback API error: HTTP {fallback_response.status_code}")
                    return ""

                candidate = fallback_response.json()
                if candidate.get("code") != 0:
                    logger.error(f"Wiki fallback API error: {candidate.get('msg', 'Unknown error')}")
                    return ""
                node_result = candidate

            node_data = node_result.get("data", {}).get("node", {})
            wiki_title = str(node_data.get("title") or "").strip()
            obj_type = str(node_data.get("obj_type") or "").strip().lower()
            obj_token = str(node_data.get("obj_token") or "").strip()

            logger.info(
                "Resolved wiki node: wiki_token=%s obj_type=%s obj_token=%s title=%s",
                doc_id,
                obj_type,
                obj_token,
                wiki_title,
            )

            if obj_type and obj_type != "docx":
                logger.warning("Wiki node %s resolved to non-docx object type: %s", doc_id, obj_type)

            if not obj_token:
                return wiki_title

            docx_token = obj_token

        raw_content_url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{docx_token}/raw_content"
        raw_content_response = http_requests.get(raw_content_url, headers=headers, timeout=15)

        if raw_content_response.status_code == 200:
            raw_content_result = raw_content_response.json()
            if raw_content_result.get("code") == 0:
                data = raw_content_result.get("data", {})
                raw_content = (
                    data.get("content")
                    or data.get("raw_content")
                    or data.get("text")
                    or ""
                )
                if isinstance(raw_content, str):
                    content = raw_content.strip()

        if not content:
            content_url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{docx_token}/content"
            content_response = http_requests.get(content_url, headers=headers, timeout=15)

            if content_response.status_code != 200:
                logger.error(f"Docx API error: HTTP {content_response.status_code}")
                return wiki_title

            content_result = content_response.json()
            if content_result.get("code") != 0:
                logger.error(f"Docx API error: {content_result.get('msg', 'Unknown error')}")
                return wiki_title

            content = _extract_docx_text(content_result.get("data", {})).strip()

        if wiki_title and content and wiki_title not in content:
            content = f"{wiki_title}\n{content}".strip()
        elif wiki_title and not content:
            content = wiki_title

        final_content = content.strip()
        logger.info(
            "Fetched Feishu document content: url=%s length=%s content=%s",
            url,
            len(final_content),
            final_content,
        )
        return final_content

    except Exception as e:
        logger.error(f"Failed to fetch content from {url}: {e}")
        return ""


def fetch_feishu_doc_images(url: str, app_id: str, app_secret: str,
                            output_dir: Optional[str] = None) -> Dict[str, Any]:
    """Download image assets embedded in a Feishu docx/wiki document."""
    resolved_url = resolve_feishu_doc_url(url)
    if not resolved_url:
        return {"url": "", "image_paths": [], "errors": ["无法解析飞书文档链接"]}

    try:
        token = get_feishu_token(app_id, app_secret)
        if not token:
            return {"url": resolved_url, "image_paths": [], "errors": ["获取飞书访问令牌失败"]}

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        docx_token, _ = _resolve_feishu_docx_identity(resolved_url, headers)
        if not docx_token:
            return {"url": resolved_url, "image_paths": [], "errors": ["无法解析文档 token"]}

        blocks_result = _fetch_feishu_doc_blocks(docx_token, headers)
        if blocks_result.get("errors"):
            return {
                "url": resolved_url,
                "image_paths": [],
                "errors": blocks_result["errors"],
            }

        image_token_meta = _extract_docx_image_tokens_for_fill(blocks_result.get("blocks") or [])
        file_tokens = image_token_meta.get("all_tokens") or []
        fill_tokens = image_token_meta.get("fill_tokens") or []
        cover_tokens = image_token_meta.get("cover_tokens") or []
        body_tokens = image_token_meta.get("body_tokens") or []
        logger.info("Extracted %s candidate image tokens from doc %s", len(file_tokens), docx_token)
        if not file_tokens:
            return {
                "url": resolved_url,
                "image_paths": [],
                "fill_image_paths": [],
                "cover_image_paths": [],
                "body_image_paths": [],
                "errors": [],
            }

        if output_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            output_dir = os.path.join(base_dir, "results", "feishu_images", docx_token)
        os.makedirs(output_dir, exist_ok=True)

        image_paths = []
        errors = []
        token_to_path: Dict[str, str] = {}
        for index, file_token in enumerate(file_tokens, start=1):
            tmp_url = _get_feishu_tmp_download_url(file_token, headers)
            if not tmp_url:
                errors.append(f"图片素材下载地址获取失败: {file_token}")
                continue

            try:
                download_response = http_requests.get(tmp_url, timeout=30)
                if download_response.status_code != 200:
                    errors.append(f"图片下载失败 {file_token}: HTTP {download_response.status_code}")
                    continue
                content_type = download_response.headers.get("Content-Type", "").split(";")[0].strip()
                suffix = mimetypes.guess_extension(content_type) or ".png"
                local_path = os.path.join(output_dir, f"image_{index:03d}{suffix}")
                with open(local_path, "wb") as image_file:
                    image_file.write(download_response.content)
                image_paths.append(local_path)
                token_to_path[file_token] = local_path
            except Exception as exc:  # pragma: no cover - network issues are environmental
                errors.append(f"图片下载异常 {file_token}: {exc}")

        fill_image_paths = [token_to_path[token] for token in fill_tokens if token in token_to_path]
        cover_image_paths = [token_to_path[token] for token in cover_tokens if token in token_to_path]
        body_image_paths = [token_to_path[token] for token in body_tokens if token in token_to_path]

        return {
            "url": resolved_url,
            "image_paths": image_paths,
            "fill_image_paths": fill_image_paths,
            "cover_image_paths": cover_image_paths,
            "body_image_paths": body_image_paths,
            "errors": errors,
        }
    except Exception as e:
        logger.error(f"Failed to fetch doc images from {resolved_url}: {e}")
        return {
            "url": resolved_url,
            "image_paths": [],
            "fill_image_paths": [],
            "cover_image_paths": [],
            "body_image_paths": [],
            "errors": [str(e)],
        }


def download_feishu_doc_snapshot(url: str, app_id: str, app_secret: str, output_dir: Optional[str] = None) -> Dict[str, str]:
    """抓取飞书文档正文并缓存到本地文本文件。

    只要能从输入值中解析出 `docx/wiki` 链接，就会访问飞书 OpenAPI 读取正文，
    并将文本快照落盘，供后续审核或排障使用。
    """
    resolved_url = resolve_feishu_doc_url(url)
    if not resolved_url:
        return {"url": "", "content": "", "local_path": ""}

    content = fetch_feishu_content(resolved_url, app_id, app_secret)
    if not content.strip():
        return {"url": resolved_url, "content": "", "local_path": ""}

    if output_dir is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output_dir = os.path.join(base_dir, "results", "feishu_cache")

    os.makedirs(output_dir, exist_ok=True)

    parsed = urlparse(resolved_url)
    doc_kind = "wiki" if "/wiki/" in parsed.path else "docx"
    doc_token = parsed.path.rstrip("/").split("/")[-1]
    safe_token = re.sub(r"[^A-Za-z0-9_-]+", "_", doc_token) or hashlib.md5(resolved_url.encode("utf-8")).hexdigest()
    local_path = os.path.join(output_dir, f"{doc_kind}_{safe_token}.txt")

    with open(local_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info(f"Cached Feishu document snapshot: {resolved_url} -> {local_path}")
    return {"url": resolved_url, "content": content, "local_path": local_path}


def _resolve_feishu_docx_identity(url: str, headers: Dict[str, str]) -> Tuple[str, str]:
    """Resolve a doc or wiki URL to a docx token and optional wiki title."""
    doc_id = None
    is_wiki = False

    if '/docx/' in url:
        doc_id = url.split('/docx/')[1].split('?')[0].split('#')[0]
    elif '/wiki/' in url:
        doc_id = url.split('/wiki/')[1].split('?')[0].split('#')[0]
        is_wiki = True

    if not doc_id:
        return "", ""

    if not is_wiki:
        return doc_id, ""

    node_result = None
    wiki_title = ""
    node_api_url = f"https://open.feishu.cn/open-apis/wiki/v2/nodes/{doc_id}"
    node_response = http_requests.get(node_api_url, headers=headers, timeout=15)

    if node_response.status_code == 200:
        candidate = node_response.json()
        if candidate.get("code") == 0:
            node_result = candidate
    else:
        logger.warning("Wiki node API returned HTTP %s, falling back to spaces/get_node", node_response.status_code)

    if node_result is None:
        fallback_api_url = "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node"
        fallback_response = http_requests.get(
            fallback_api_url,
            headers=headers,
            params={"token": doc_id},
            timeout=15,
        )
        if fallback_response.status_code != 200:
            return "", ""
        candidate = fallback_response.json()
        if candidate.get("code") != 0:
            return "", ""
        node_result = candidate

    node_data = node_result.get("data", {}).get("node", {})
    wiki_title = str(node_data.get("title") or "").strip()
    return str(node_data.get("obj_token") or "").strip(), wiki_title


def _get_feishu_tmp_download_url(file_token: str, headers: Dict[str, str]) -> str:
    download_url = "https://open.feishu.cn/open-apis/drive/v1/medias/batch_get_tmp_download_url"
    response = http_requests.get(
        download_url,
        headers=headers,
        params={"file_tokens": file_token},
        timeout=20,
    )
    if response.status_code != 200:
        logger.warning("Feishu media tmp download API returned HTTP %s for token=%s", response.status_code, file_token)
        return ""
    result = response.json()
    if result.get("code") != 0:
        logger.warning("Feishu media tmp download API failed for token=%s: %s", file_token, result.get("msg"))
        return ""
    items = result.get("data", {}).get("tmp_download_urls") or []
    if not items:
        return ""
    return str(items[0].get("tmp_download_url") or "").strip()


def _extract_docx_image_tokens(docx_data: Dict[str, Any]) -> List[str]:
    """Best-effort extraction of image file tokens from docx block payload."""
    tokens: List[str] = []
    candidate_keys = ("file_token", "image_token", "src_token", "token")

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            # Common Feishu shape: {"image": {... file_token ...}}
            if "image" in node and isinstance(node.get("image"), dict):
                image_data = node["image"]
                for key in candidate_keys:
                    value = str(image_data.get(key) or "").strip()
                    if _looks_like_media_token(value):
                        tokens.append(value)
                        break

            # Fallback: some docx payloads place token fields directly on the block
            # or inside nested media structures rather than under `image`.
            for key in candidate_keys:
                value = str(node.get(key) or "").strip()
                if _looks_like_media_token(value):
                    lower_keys = {str(k).lower() for k in node.keys()}
                    if (
                        "image" in lower_keys
                        or "block_type" in lower_keys
                        or any("image" in name or "media" in name or "file" in name for name in lower_keys)
                    ):
                        tokens.append(value)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(docx_data.get("document", {}).get("body", {}).get("blocks", []))
    deduped = []
    for token in tokens:
        if token and token not in deduped:
            deduped.append(token)
    return deduped


def _extract_docx_image_tokens_from_blocks(blocks: List[Dict[str, Any]]) -> List[str]:
    wrapped = {"document": {"body": {"blocks": blocks}}}
    return _extract_docx_image_tokens(wrapped)


def _extract_block_plain_text(block: Dict[str, Any]) -> str:
    text_node = None
    for key in ("text", "heading1", "heading2", "heading3", "bullet"):
        candidate = block.get(key)
        if isinstance(candidate, dict):
            text_node = candidate
            break
    if not isinstance(text_node, dict):
        return ""

    parts: List[str] = []
    for element in text_node.get("elements") or []:
        text_run = element.get("text_run") or {}
        content = str(text_run.get("content") or "")
        if content:
            parts.append(content)
    return "".join(parts).strip()


def _extract_docx_image_tokens_for_fill(blocks: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """提取适合回填到表格中的图片 token。

    规则：
    - 正文配图按文档出现顺序保留
    - 封面图单独识别
    - 回填顺序为 封面图优先，其后是正文配图
    - 评论区后的图片、成片/脚本等后续模块中的图片默认不参与回填
    """
    all_tokens = _extract_docx_image_tokens_from_blocks(blocks)
    if not blocks:
        return {
            "all_tokens": all_tokens,
            "fill_tokens": all_tokens,
            "cover_tokens": [],
            "body_tokens": all_tokens,
        }

    zone = "other"
    body_tokens: List[str] = []
    cover_tokens: List[str] = []

    def _dedupe(values: List[str]) -> List[str]:
        result: List[str] = []
        for value in values:
            if value and value not in result:
                result.append(value)
        return result

    def _is_major_section_boundary(text: str) -> bool:
        if not text:
            return False
        if re.match(r"^[一二三四五六七八九十]+、", text):
            return True
        if re.match(r"^[0-9]+[、.]", text):
            return True
        return any(
            keyword in text
            for keyword in ("创意概述", "内容概述", "参考视频", "脚本文案", "脚本内容", "视频时长", "视频尺寸")
        )

    for block in blocks:
        line = _extract_block_plain_text(block)
        normalized = re.sub(r"\s+", "", line.replace("：", ":"))

        if "评论" in normalized and ":" in normalized:
            zone = "comment"
        elif "评论" in normalized:
            zone = "comment"
        elif normalized.startswith("封面:") or normalized == "封面":
            zone = "cover"
        elif normalized.startswith(("视频成片:", "成片:", "视频成片", "成片")):
            zone = "asset"
        elif _is_major_section_boundary(normalized):
            zone = "other"
        elif any(keyword in normalized for keyword in ("笔记配图", "笔记图片", "配图", "图片", "发布文案+成片", "发布文案", "正文", "标题")):
            zone = "body"

        if block.get("block_type") != 27:
            continue

        image_node = block.get("image") or {}
        token = str(
            image_node.get("token")
            or image_node.get("file_token")
            or image_node.get("image_token")
            or ""
        ).strip()
        if not _looks_like_media_token(token):
            continue

        if zone == "cover":
            cover_tokens.append(token)
        elif zone == "body":
            body_tokens.append(token)

    cover_tokens = _dedupe(cover_tokens)
    body_tokens = _dedupe(body_tokens)
    fill_tokens = _dedupe(cover_tokens + body_tokens)

    if not fill_tokens:
        fill_tokens = all_tokens
        body_tokens = all_tokens

    return {
        "all_tokens": all_tokens,
        "fill_tokens": fill_tokens,
        "cover_tokens": cover_tokens,
        "body_tokens": body_tokens,
    }


def upload_feishu_bitable_media(file_path: str, app_token: str, app_id: str, app_secret: str) -> Dict[str, Any]:
    """上传本地图片到飞书，返回可写入 bitable 附件列的文件信息。"""
    try:
        token = get_feishu_token(app_id, app_secret)
        if not token:
            return {"ok": False, "error": "获取飞书访问令牌失败"}

        path = os.path.abspath(file_path)
        if not os.path.exists(path):
            return {"ok": False, "error": f"文件不存在: {path}"}

        file_name = os.path.basename(path)
        file_size = os.path.getsize(path)
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        upload_url = "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all"
        headers = {"Authorization": f"Bearer {token}"}

        with open(path, "rb") as file_obj:
            response = http_requests.post(
                upload_url,
                headers=headers,
                data={
                    "file_name": file_name,
                    "parent_type": "bitable_file",
                    "parent_node": app_token,
                    "size": str(file_size),
                },
                files={"file": (file_name, file_obj, content_type)},
                timeout=60,
            )

        if response.status_code != 200:
            return {"ok": False, "error": f"飞书上传图片失败: HTTP {response.status_code}"}

        result = response.json()
        if result.get("code") != 0:
            return {"ok": False, "error": f"飞书上传图片失败: {result.get('msg', 'Unknown error')}"}

        file_token = str(result.get("data", {}).get("file_token") or "").strip()
        if not file_token:
            return {"ok": False, "error": "飞书上传图片成功但未返回 file_token"}

        return {
            "ok": True,
            "file_token": file_token,
            "name": file_name,
            "size": file_size,
            "type": content_type,
        }
    except Exception as e:
        logger.error("Failed to upload media to Feishu bitable: %s", e)
        return {"ok": False, "error": str(e)}


def _fetch_feishu_doc_blocks(docx_token: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """Fetch all docx blocks via the official blocks API."""
    blocks = []
    page_token = ""
    errors: List[str] = []

    for _ in range(20):
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token

        response = http_requests.get(
            f"https://open.feishu.cn/open-apis/docx/v1/documents/{docx_token}/blocks",
            headers=headers,
            params=params,
            timeout=20,
        )
        if response.status_code != 200:
            errors.append(f"文档 blocks 接口返回 HTTP {response.status_code}")
            break

        payload = response.json()
        if payload.get("code") != 0:
            errors.append(f"文档 blocks 接口报错: {payload.get('msg', 'Unknown error')}")
            break

        data = payload.get("data", {})
        page_items = data.get("items") or []
        blocks.extend(page_items)

        has_more = bool(data.get("has_more"))
        page_token = str(data.get("page_token") or "")
        if not has_more:
            break
    else:
        errors.append("文档 blocks 分页超过上限，已中止")

    logger.info("Fetched %s doc blocks for %s", len(blocks), docx_token)
    return {"blocks": blocks, "errors": errors}


def _looks_like_media_token(value: str) -> bool:
    if not value:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return False
    # Exclude obviously non-media scalar values that happen to use token field names.
    return not value.lower().startswith(("wiki", "docx", "sheet", "table", "view"))


def _extract_docx_text(docx_data: Dict[str, Any]) -> str:
    """从飞书docx数据中提取纯文本"""
    text_parts = []

    try:
        document = docx_data.get("document", {})
        body = document.get("body", {})
        blocks = body.get("blocks", [])

        for block in blocks:
            block_type = block.get("block_type")

            if block_type == "text":
                # 处理文本块
                text_elements = block.get("text", {}).get("elements", [])
                for element in text_elements:
                    if element.get("type") == "text_run":
                        content = element.get("text_run", {}).get("content", "")
                        text_parts.append(content)

            elif block_type == "heading1":
                # 处理一级标题
                text_elements = block.get("heading1", {}).get("elements", [])
                heading_text = ""
                for element in text_elements:
                    if element.get("type") == "text_run":
                        content = element.get("text_run", {}).get("content", "")
                        heading_text += content
                if heading_text:
                    text_parts.append(f"\n# {heading_text}\n")

            elif block_type == "heading2":
                # 处理二级标题
                text_elements = block.get("heading2", {}).get("elements", [])
                heading_text = ""
                for element in text_elements:
                    if element.get("type") == "text_run":
                        content = element.get("text_run", {}).get("content", "")
                        heading_text += content
                if heading_text:
                    text_parts.append(f"\n## {heading_text}\n")

            elif block_type == "heading3":
                # 处理三级标题
                text_elements = block.get("heading3", {}).get("elements", [])
                heading_text = ""
                for element in text_elements:
                    if element.get("type") == "text_run":
                        content = element.get("text_run", {}).get("content", "")
                        heading_text += content
                if heading_text:
                    text_parts.append(f"\n### {heading_text}\n")

            elif block_type == "bullet_list":
                # 处理列表
                list_items = block.get("bullet_list", {}).get("elements", [])
                for item in list_items:
                    item_text = ""
                    elements = item.get("elements", [])
                    for element in elements:
                        if element.get("type") == "text_run":
                            content = element.get("text_run", {}).get("content", "")
                            item_text += content
                    if item_text:
                        text_parts.append(f"• {item_text}")

        return "\n".join(text_parts)

    except Exception as e:
        logger.error(f"Failed to extract text from docx data: {e}")
        return ""


def validate_feishu_config(app_id: str, app_secret: str) -> Tuple[bool, str]:
    """验证飞书配置是否有效

    Args:
        app_id: 飞书应用ID
        app_secret: 飞书应用密钥

    Returns:
        tuple: (是否有效, 错误信息)
    """
    try:
        token = get_feishu_token(app_id, app_secret)
        if token:
            return True, "配置有效"
        else:
            return False, "无法获取访问令牌"
    except Exception as e:
        return False, f"配置验证失败: {str(e)}"
