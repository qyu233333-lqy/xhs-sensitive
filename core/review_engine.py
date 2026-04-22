"""审核引擎核心模块"""

import json
import logging
import os
import re
import time
from typing import Dict, List, Any, Generator

import anthropic

from .config import load_config
from .project import get_project_config_exact_for_review
from .feishu import (
    add_feishu_comment,
    download_feishu_doc_snapshot,
    fetch_feishu_content,
    write_bitable_records,
    write_feishu_sheet,
)
from .file_utils import save_results_to_excel, get_hyperlink_for_cell

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PDF = os.getenv("DEFAULT_PDF_PATH", os.path.join(BASE_DIR, "审核规则.pdf"))


def get_rules() -> str:
    """获取审核规则，优先从PDF文件读取"""
    from .file_utils import read_pdf_content

    try:
        if os.path.exists(DEFAULT_PDF):
            rules = read_pdf_content(DEFAULT_PDF)
            if rules.strip():
                logger.debug(f"Loaded rules from PDF: {len(rules)} characters")
                return rules

        # 如果PDF不存在或为空，返回默认规则
        from .audit_engine import REVIEW_SYSTEM
        logger.debug("Using default review rules")
        return REVIEW_SYSTEM

    except Exception as e:
        logger.warning(f"Failed to load rules from PDF: {e}, using default")
        from .audit_engine import REVIEW_SYSTEM
        return REVIEW_SYSTEM


def process_review_task(task_data: Dict[str, Any]) -> Generator[Dict[str, Any], None, None]:
    """处理审核任务，生成进度更新

    Args:
        task_data: 任务数据字典

    Yields:
        dict: 进度更新信息
    """
    try:
        # 加载配置
        config = load_config()
        if not config.get("api_key"):
            raise ValueError("缺少API密钥配置")

        # 初始化Anthropic客户端
        client_kwargs = {"api_key": config["api_key"]}
        if config.get("base_url"):
            client_kwargs["base_url"] = config["base_url"]

        client = anthropic.Anthropic(**client_kwargs)
        model = config.get("model", "claude-sonnet-4-20250514")

        # 获取数据
        data_rows = task_data["data"]["data"]
        total_rows = len(data_rows)

        yield {
            "type": "status",
            "message": f"开始处理 {total_rows} 条数据",
            "progress": 0,
            "total": total_rows
        }

        # 处理每一行
        results = []
        for i, row in enumerate(data_rows):
            try:
                progress = ((i + 1) / total_rows) * 100
                yield {
                    "type": "progress",
                    "current": i + 1,
                    "total": total_rows,
                    "progress": progress,
                    "message": f"处理第 {i + 1}/{total_rows} 条"
                }

                # 处理单行数据
                result = _process_row(client, model, row, task_data, config)
                results.append(result)

                # 发送行处理结果
                yield {
                    "type": "item_done",
                    "current": i + 1,
                    "total": total_rows,
                    "result": _format_result_item(result, i + 1)
                }

            except Exception as e:
                logger.error(f"Failed to process row {i + 1}: {e}")
                error_result = _create_error_result(row, str(e))
                results.append(error_result)

                yield {
                    "type": "item_done",
                    "current": i + 1,
                    "total": total_rows,
                    "result": _format_result_item(error_result, i + 1)
                }

        # 保存结果
        yield {
            "type": "status",
            "message": "保存审核结果...",
            "progress": 95
        }

        save_meta = _save_results(task_data, results, config)

        yield {
            "type": "done",
            "msg": f"审核完成，结果已保存到 {save_meta['output_file']}",
            "output_file": save_meta["output_file"],
            "source": "feishu" if task_data["type"] == "feishu_url" else "excel",
            "write_error": save_meta.get("write_error"),
            **_build_summary(results)
        }

    except Exception as e:
        logger.error(f"Review task failed: {e}")
        yield {
            "type": "error",
            "msg": f"审核任务失败: {str(e)}"
        }


def _process_row(client, model: str, row: Dict[str, Any], task_data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """处理单行数据"""
    try:
        logger.info(
            "Review row payload: %s",
            json.dumps(row, ensure_ascii=False, default=str),
        )

        if _is_already_reviewed(row):
            return _create_existing_review_skip_result(row)

        content_link = _extract_url_from_cell(row.get("稿件链接"))
        project_name = _cell_to_plain_text(row.get("项目名称") or row.get("权益类型")).strip()
        slogan_word = _cell_to_plain_text(row.get("口令词")).strip()
        raw_link_text = _cell_to_plain_text(row.get("稿件链接")).strip()

        logger.info(
            "Resolved稿件链接: record_id=%s note_id=%s project=%s raw=%s resolved=%s",
            row.get("_record_id", ""),
            row.get("笔记编号", ""),
            project_name,
            raw_link_text,
            content_link,
        )

        # 获取内容。优先读取稿件链接中的飞书文档，若为空则回退到表格中的标题/文案/评论区文案。
        content_parts = []
        if content_link:
            if task_data["type"] == "feishu_url" and any(domain in content_link for domain in ["feishu.cn", "larksuite.com"]):
                snapshot = download_feishu_doc_snapshot(
                    content_link,
                    config.get("feishu_app_id", ""),
                    config.get("feishu_app_secret", "")
                )
                linked_content = snapshot.get("content", "")
                if linked_content.strip():
                    content_parts.append(linked_content)
            elif task_data["type"] == "excel_upload":
                hyperlinks = task_data["data"].get("hyperlinks", {})
                hyperlink_url = get_hyperlink_for_cell(
                    hyperlinks,
                    row.get("_row_index", 1),
                    "稿件链接",
                    task_data["data"]["headers"]
                )
                if hyperlink_url and any(domain in hyperlink_url for domain in ["feishu.cn", "larksuite.com"]):
                    snapshot = download_feishu_doc_snapshot(
                        hyperlink_url,
                        config.get("feishu_app_id", ""),
                        config.get("feishu_app_secret", "")
                    )
                    linked_content = snapshot.get("content", "")
                    if linked_content.strip():
                        content_parts.append(linked_content)
                else:
                    content_parts.append(content_link)

        for field_name in ["标题", "文案", "评论区文案", "稿件链接"]:
            text = _cell_to_plain_text(row.get(field_name)).strip()
            if text:
                content_parts.append(text)

        deduped_parts = []
        for part in content_parts:
            if part and part not in deduped_parts:
                deduped_parts.append(part)
        content = "\n\n".join(deduped_parts)

        if raw_link_text and not content_link and not content.strip():
            return _create_error_result(row, "稿件链接列不是可访问的飞书文档链接，无法读取文章内容")

        if not content.strip():
            return _create_empty_content_result(row)

        project_config = get_project_config_exact_for_review(project_name) if project_name else None
        overall_passed, violations, audit_notes = _audit_row_against_project(
            content=content,
            slogan_word=slogan_word,
            project_name=project_name,
            project_config=project_config,
        )

        # 构建结果
        result = row.copy()
        result["稿件内容"] = content[:500] + "..." if len(content) > 500 else content
        result["AI审核"] = "✅" if overall_passed else "❌"
        result["AI审核状态（内部）"] = "已过审" if overall_passed else "未过审"
        result["违规原因"] = violations
        result["审核备注"] = audit_notes
        result["处理时间"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # 如果审核失败，添加飞书评论（如果配置了飞书）
        if not overall_passed and content_link and config.get("feishu_app_id"):
            comment = f"未过审原因：{violations}\n\n审核时间：{result['处理时间']}"
            add_feishu_comment(
                content_link,
                config.get("feishu_app_id", ""),
                config.get("feishu_app_secret", ""),
                comment
            )

        return result

    except Exception as e:
        logger.error(f"Failed to process single row: {e}")
        return _create_error_result(row, str(e))


def _combine_audit_results(audit_results: Dict[str, Any], project_config: Dict[str, str] = None) -> tuple:
    """综合多种审核结果，返回最终判断

    Returns:
        tuple: (overall_passed, violations, audit_notes)
    """
    overall_passed = True
    violations = []
    audit_notes_parts = []

    # 项目特定审核结果
    if "project_audit" in audit_results:
        project_audit = audit_results["project_audit"]
        if not project_audit.get("overall_passed", True):
            overall_passed = False
            violations.extend(project_audit.get("overall_violations", []))

        audit_notes_parts.append(f"项目审核({project_config.get('项目名称', 'Unknown')}): {project_audit.get('summary', '')}")

    # 通用审核结果
    if "general_audit" in audit_results:
        general_audit = audit_results["general_audit"]
        if not general_audit.get("passed", True):
            overall_passed = False
            if general_audit.get("violations"):
                violations.extend(general_audit["violations"])

        if general_audit.get("notes"):
            audit_notes_parts.append(f"通用审核: {general_audit['notes']}")

    # 如果没有任何审核结果，默认通过
    if not audit_results:
        audit_notes_parts.append("未执行任何审核")

    violations_text = "; ".join(violations) if violations else ""
    audit_notes = " | ".join(audit_notes_parts)

    return overall_passed, violations_text, audit_notes


def _create_empty_content_result(row: Dict[str, Any]) -> Dict[str, Any]:
    """创建空内容的审核结果"""
    result = row.copy()
    result["稿件内容"] = ""
    result["AI审核"] = "⏭️"
    result["AI审核状态（内部）"] = "跳过审核"
    result["违规原因"] = ""
    result["审核备注"] = "内容为空，跳过审核"
    result["处理时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return result


def _create_existing_review_skip_result(row: Dict[str, Any]) -> Dict[str, Any]:
    """对已审核稿件直接跳过，并禁止写回覆盖原状态。"""
    result = row.copy()
    result["稿件内容"] = ""
    result["AI审核"] = "⏭️"
    result["AI审核状态（内部）"] = row.get("AI审核状态（内部）") or row.get("AI审核") or "已审核"
    result["违规原因"] = ""
    result["审核备注"] = "已审核：跳过"
    result["处理时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
    result["_skip_writeback"] = True
    return result


def _create_error_result(row: Dict[str, Any], error_msg: str) -> Dict[str, Any]:
    """创建错误情况的审核结果"""
    result = row.copy()
    result["稿件内容"] = ""
    result["AI审核"] = "❌"
    result["AI审核状态（内部）"] = "未过审"
    result["违规原因"] = ""
    result["审核备注"] = f"处理失败: {error_msg}"
    result["处理时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return result


def _save_results(task_data: Dict[str, Any], results: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    """保存审核结果"""
    try:
        task_id = task_data["task_id"]
        timestamp = time.strftime("%Y%m%d_%H%M%S")

        # 生成输出文件名
        if task_data["type"] == "feishu_url":
            output_filename = f"{task_id}_feishu_results_{timestamp}.xlsx"
        else:
            output_filename = f"{task_id}_excel_results_{timestamp}.xlsx"

        output_path = os.path.join(BASE_DIR, "results", output_filename)

        # 准备表头
        if results:
            # 确保关键列在前面
            priority_headers = ["昵称", "稿件链接", "权益类型", "AI审核", "违规原因", "审核备注", "稿件内容", "处理时间"]
            all_headers = list(results[0].keys())

            # 重新排序表头
            headers = []
            for h in priority_headers:
                if h in all_headers:
                    headers.append(h)
                    all_headers.remove(h)
            headers.extend(all_headers)  # 添加剩余的列

            # 保存到Excel
            success = save_results_to_excel(results, output_path, headers)
            if not success:
                raise ValueError("保存Excel文件失败")

            write_error = None

            # 如果是飞书表格，尝试写回原表格
            if (task_data["type"] == "feishu_url" and
                config.get("feishu_app_id") and
                config.get("feishu_app_secret")):

                try:
                    _write_back_to_feishu(task_data, results, config)
                except Exception as e:
                    write_error = str(e)
                    logger.warning(f"Failed to write back to Feishu: {e}")

            return {"output_file": output_filename, "write_error": write_error}

        else:
            raise ValueError("没有结果数据可保存")

    except Exception as e:
        logger.error(f"Failed to save results: {e}")
        raise


def _write_back_to_feishu(task_data: Dict[str, Any], results: List[Dict[str, Any]], config: Dict[str, Any]):
    """将结果写回飞书表格"""
    try:
        sheet_data = task_data["data"]
        if sheet_data.get("is_bitable"):
            _write_back_to_bitable(sheet_data, results, config)
            return

        spreadsheet_id = sheet_data["spreadsheet_id"]
        sheet_id = sheet_data["sheet_id"]

        # 准备批量更新数据
        updates = []
        headers = sheet_data["headers"]

        # 找到目标列的索引
        ai_audit_col = None
        ai_audit_internal_col = None  # 专门处理"AI审核状态（内部）"字段
        violation_col = None
        notes_col = None

        for i, header in enumerate(headers):
            if "AI审核状态（内部）" in header:
                ai_audit_internal_col = i
            elif "AI审核" in header or "审核结果" in header:
                ai_audit_col = i
            elif "违规" in header or "原因" in header:
                violation_col = i
            elif "备注" in header or "说明" in header:
                notes_col = i

        # 如果没有找到任何审核列，跳过写回
        if ai_audit_internal_col is None and ai_audit_col is None:
            logger.warning("未找到AI审核列，跳过写回飞书")
            return

        # 准备更新数据
        for i, result in enumerate(results):
            if result.get("_skip_writeback"):
                continue
            row_index = i + 2  # Excel行号（跳过表头）

            # 优先处理"AI审核状态（内部）"列，写入文字状态
            if ai_audit_internal_col is not None:
                col_letter = chr(65 + ai_audit_internal_col)
                range_ref = f"{col_letter}{row_index}"
                # 将符号转换为文字状态
                audit_status = result.get("AI审核", "")
                if audit_status == "✅":
                    status_text = "已过审"
                elif audit_status == "❌":
                    status_text = "未过审"
                elif audit_status == "⏭️":
                    status_text = "跳过审核"
                else:
                    status_text = "未知状态"

                updates.append({
                    "range": range_ref,
                    "values": [[status_text]]
                })
            # 如果没有"AI审核状态（内部）"列，则写入普通AI审核列
            elif ai_audit_col is not None:
                col_letter = chr(65 + ai_audit_col)  # A, B, C, ...
                range_ref = f"{col_letter}{row_index}"
                updates.append({
                    "range": range_ref,
                    "values": [[result.get("AI审核", "")]]
                })

            # 违规原因列
            if violation_col is not None and result.get("违规原因"):
                col_letter = chr(65 + violation_col)
                range_ref = f"{col_letter}{row_index}"
                updates.append({
                    "range": range_ref,
                    "values": [[result.get("违规原因", "")]]
                })

            # 备注列
            if notes_col is not None and result.get("审核备注"):
                col_letter = chr(65 + notes_col)
                range_ref = f"{col_letter}{row_index}"
                updates.append({
                    "range": range_ref,
                    "values": [[result.get("审核备注", "")]]
                })

        # 执行批量更新
        if updates:
            success = write_feishu_sheet(
                spreadsheet_id, sheet_id,
                config["feishu_app_id"],
                config["feishu_app_secret"],
                updates
            )

            if success:
                logger.info(f"Successfully wrote back {len(updates)} updates to Feishu")
            else:
                logger.warning("Failed to write back to Feishu")

    except Exception as e:
        logger.error(f"Error writing back to Feishu: {e}")
        raise


def _write_back_to_bitable(sheet_data: Dict[str, Any], results: List[Dict[str, Any]], config: Dict[str, Any]):
    """将结果写回飞书多维表格。"""
    app_token = sheet_data.get("app_token") or sheet_data.get("spreadsheet_id")
    table_id = sheet_data["sheet_id"]
    headers = set(sheet_data.get("headers", []))

    if "AI审核状态（内部）" not in headers:
        raise ValueError("当前表格不存在 AI审核状态（内部） 列，拒绝写回")

    updates = []
    for result in results:
        if result.get("_skip_writeback"):
            continue
        record_id = result.get("_record_id")
        if not record_id:
            continue

        audit_status = result.get("AI审核", "")
        if audit_status == "✅":
            status_text = "已过审"
        elif audit_status == "❌":
            status_text = "未过审"
        elif audit_status == "⏭️":
            status_text = "跳过审核"
        else:
            status_text = "未知状态"

        updates.append({
            "record_id": record_id,
            "fields": {"AI审核状态（内部）": status_text}
        })

    if not updates:
        logger.warning("No bitable record updates prepared")
        return

    success = write_bitable_records(
        app_token,
        table_id,
        config["feishu_app_id"],
        config["feishu_app_secret"],
        updates
    )

    if success:
        logger.info(f"Successfully wrote back {len(updates)} updates to Feishu bitable")
    else:
        raise ValueError("写回飞书多维表格失败")


def _cell_to_plain_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or value.get("url") or "")
    if isinstance(value, list):
        return "; ".join([part for part in (_cell_to_plain_text(item) for item in value) if part])
    return str(value)


def _extract_url_from_cell(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("link") or value.get("url") or "")
    if isinstance(value, list):
        for item in value:
            extracted = _extract_url_from_cell(item)
            if extracted:
                return extracted
    text = _cell_to_plain_text(value)
    match = re.search(r'https?://\S+', text)
    return match.group(0) if match else ""


def _is_already_reviewed(row: Dict[str, Any]) -> bool:
    """判断稿件是否已过审，已过审才跳过。"""
    audit_values = [
        _cell_to_plain_text(row.get("AI审核状态（内部）")).strip(),
        _cell_to_plain_text(row.get("AI审核")).strip(),
        _cell_to_plain_text(row.get("审核结果")).strip(),
    ]
    for value in audit_values:
        if value and "已过审" in value:
            return True
    return False


def _normalize_for_match(text: str) -> str:
    return re.sub(r'[\s\u3000“”"\'：:，,。！？!?.、—\-（）()【】\[\]]+', '', text or "").lower()


def _contains_required_text(content: str, required_text: str) -> bool:
    required_text = (required_text or "").strip()
    if not required_text:
        return True
    if required_text in content:
        return True
    return _normalize_for_match(required_text) in _normalize_for_match(content)


def _benefit_requirement_matched(content: str, requirement: str) -> bool:
    requirement = (requirement or "").strip()
    if not requirement:
        return True
    if _contains_required_text(content, requirement):
        return True

    normalized_content = _normalize_for_match(content)

    if "白银及以上" in requirement:
        if any(keyword in content for keyword in ["黑钻会员", "高等级会员", "钻石会员", "铂金会员", "黄金会员"]):
            return True

    semantic_alias_groups = [
        (["按等级叠加解锁"], ["权益随等级递增", "按等级递增", "随等级递增", "等级递增"]),
        (["最高85折"], ["85折", "最低85折", "超优惠价格85折", "优惠价格85折"]),
    ]
    for requirement_aliases, content_aliases in semantic_alias_groups:
        if any(alias in requirement for alias in requirement_aliases):
            if any(alias in content for alias in content_aliases):
                return True

    tokens = [
        token.strip()
        for token in re.split(r"[，,、；;。.\s]+", requirement)
        if token.strip()
    ]
    meaningful_tokens = []
    for token in tokens:
        normalized = _normalize_for_match(token)
        if len(normalized) >= 3 or re.search(r"\d", token):
            meaningful_tokens.append(normalized)

    if not meaningful_tokens:
        return False

    matched = sum(1 for token in meaningful_tokens if token and token in normalized_content)
    threshold = max(1, min(len(meaningful_tokens), 2))
    return matched >= threshold




def _extract_hashtags(text: str) -> List[str]:
    return [tag.strip() for tag in re.findall(r'#[^\s#]+', text or "") if tag.strip()]


def _split_benefit_requirements(text: str) -> List[str]:
    items = []
    for part in re.split(r'[\n；;]+', text or ""):
        cleaned = part.strip(" \t\r\n-•·")
        if cleaned:
            items.append(cleaned)
    return items


def _audit_row_against_project(content: str, slogan_word: str, project_name: str,
                               project_config: Dict[str, Any] = None) -> tuple[bool, str, str]:
    """按项目名称和 CSV 规则做确定性审核。"""
    violations = []

    if not project_name:
        violations.append("项目名称为空，无法匹配审核标准")
    if not project_config:
        violations.append(f"未在CSV中找到项目“{project_name or '未知项目'}”对应的审核标准")

    if not violations:
        if slogan_word and not _contains_required_text(content, slogan_word):
            violations.append(f"口令词不一致，正文未包含“{slogan_word}”")

        for hashtag in _extract_hashtags(project_config.get("话题标签", "")):
            if not _contains_required_text(content, hashtag):
                violations.append(f"缺少话题标签：{hashtag}")

        for benefit in _split_benefit_requirements(project_config.get("利益点标准", "")):
            if not _benefit_requirement_matched(content, benefit):
                violations.append(f"缺少利益点标准：{benefit}")

    passed = len(violations) == 0
    notes = f"按项目“{project_name or '未知项目'}”审核通过" if passed else "；".join(violations)
    return passed, "；".join(violations), notes


def _format_result_item(result: Dict[str, Any], seq: int) -> Dict[str, Any]:
    """将内部结果转换为前端展示结构。"""
    audit_state = result.get("AI审核", "")
    skipped = audit_state == "⏭️"
    if skipped:
        label = "跳过"
    elif audit_state == "✅":
        label = "已过审"
    elif audit_state == "❌":
        label = "未过审" if not str(result.get("审核备注", "")).startswith("处理失败:") else "审核出错"
    else:
        label = "审核出错"

    violations_text = result.get("违规原因", "") or ""
    violations = [v.strip() for v in violations_text.split("；") if v.strip()]
    link_value = result.get("稿件链接")
    if isinstance(link_value, dict):
        link_text = str(link_value.get("text") or link_value.get("link") or "")
    elif isinstance(link_value, list):
        parts = []
        for item in link_value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("link") or ""))
            elif item:
                parts.append(str(item))
        link_text = "; ".join([part for part in parts if part])
    else:
        link_text = str(link_value or "")

    if label == "未过审":
        reason = violations_text
        violations = []
    elif label == "已过审":
        reason = f"按项目“{result.get('项目名称') or result.get('权益类型') or ''}”审核通过"
    else:
        reason = result.get("审核备注", "") or violations_text

    return {
        "seq": seq,
        "link_text": link_text,
        "project_name": result.get("项目名称") or result.get("权益类型") or "",
        "label": label,
        "reason": reason,
        "violations": violations,
        "skipped": skipped,
    }


def _build_summary(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构建前端结果页所需汇总。"""
    formatted = [_format_result_item(result, i + 1) for i, result in enumerate(results)]
    passed = sum(1 for item in formatted if item["label"] == "已过审")
    failed = sum(1 for item in formatted if item["label"] == "未过审")
    skipped = sum(1 for item in formatted if item["skipped"])

    return {
        "total": len(formatted),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "results": formatted,
    }
