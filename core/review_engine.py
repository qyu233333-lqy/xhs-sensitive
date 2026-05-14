"""审核引擎核心模块"""

import json
import logging
import os
import re
import time
import base64
import mimetypes
from typing import Dict, List, Any, Generator, Optional

import anthropic

from .config import load_config, resolve_ai_profile
from .project import get_project_config_exact_for_review
from .feishu import (
    add_feishu_comment,
    download_feishu_doc_snapshot,
    fetch_feishu_doc_images,
    fetch_feishu_content,
    write_bitable_records,
    write_feishu_sheet,
)
from .file_utils import format_audit_status_display, save_results_to_excel, get_hyperlink_for_cell
from .image_audit import run_ocr_on_images

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PDF = os.getenv("DEFAULT_PDF_PATH", os.path.join(BASE_DIR, "审核规则.pdf"))


def _normalize_audit_status(value: Any) -> str:
    """统一审核状态文本，兼容旧的图标状态值。"""
    text = _cell_to_plain_text(value).strip()
    mapping = {
        "✅": "已过审",
        "❌": "未过审",
        "⏭️": "跳过审核",
        "已通过": "已过审",
        "未通过": "未过审",
        "跳过": "跳过审核",
        "通过": "已过审",
        "审核通过": "已过审",
    }
    return mapping.get(text, text)


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
        ai_profile = resolve_ai_profile(config, task_data.get("profile_id"))
        if not ai_profile.get("api_key"):
            raise ValueError("缺少API密钥配置")
        requester = task_data.get("requested_by") or {}
        logger.info(
            "Starting review task: task_id=%s requester=%s profile=%s",
            task_data.get("task_id"),
            requester.get("display_name") or "local",
            ai_profile.get("id"),
        )

        # 初始化Anthropic客户端
        client_kwargs = {"api_key": ai_profile["api_key"]}
        if ai_profile.get("base_url"):
            client_kwargs["base_url"] = ai_profile["base_url"]

        client = anthropic.Anthropic(**client_kwargs)
        model = ai_profile.get("model", "claude-sonnet-4-20250514")

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
        resolved_link_url = _resolve_review_link_url(row, task_data, content_link)
        content_parts = []
        image_paths: List[str] = []
        image_fetch_errors: List[str] = []
        if resolved_link_url:
            if any(domain in resolved_link_url for domain in ["feishu.cn", "larksuite.com"]):
                snapshot = download_feishu_doc_snapshot(
                    resolved_link_url,
                    config.get("feishu_app_id", ""),
                    config.get("feishu_app_secret", "")
                )
                linked_content = snapshot.get("content", "")
                if linked_content.strip():
                    content_parts.append(linked_content)

                image_bundle = fetch_feishu_doc_images(
                    resolved_link_url,
                    config.get("feishu_app_id", ""),
                    config.get("feishu_app_secret", ""),
                )
                image_paths = image_bundle.get("image_paths") or []
                image_fetch_errors = image_bundle.get("errors") or []
            else:
                content_parts.append(resolved_link_url)

        for field_name in ["标题", "文案", "评论区文案", "稿件链接"]:
            text = _cell_to_plain_text(row.get(field_name)).strip()
            if text:
                content_parts.append(text)

        deduped_parts = []
        for part in content_parts:
            if part and part not in deduped_parts:
                deduped_parts.append(part)
        content = "\n\n".join(deduped_parts)

        if raw_link_text and not resolved_link_url and not content.strip():
            return _create_error_result(row, "稿件链接列不是可访问的飞书文档链接，无法读取文章内容")

        if not content.strip():
            return _create_empty_content_result(row)

        # 文字审核先独立完成，图片 OCR 只作为补充信息，失败则直接跳过。
        ocr_result = run_ocr_on_images(image_paths)
        image_text = str(ocr_result.get("merged_text") or "").strip()
        combined_content = content if not image_text else f"{content}\n\n[图片OCR]\n{image_text}"

        project_config = get_project_config_exact_for_review(project_name) if project_name else None
        overall_passed, violations, audit_notes = _audit_row_against_project(
            content=combined_content,
            slogan_word=slogan_word,
            project_name=project_name,
            project_config=project_config,
            client=client,
            model=model,
            image_paths=image_paths,
        )

        audit_notes = _append_image_audit_notes(
            audit_notes,
            image_paths=image_paths,
            image_text=image_text,
            image_fetch_errors=image_fetch_errors,
            ocr_result=ocr_result,
        )

        # 构建结果
        result = row.copy()
        result["稿件内容"] = content[:500] + "..." if len(content) > 500 else content
        result["图片OCR内容"] = image_text[:500] + "..." if len(image_text) > 500 else image_text
        result["AI审核"] = "已过审" if overall_passed else "未过审"
        result["AI审核状态（内部）"] = "已过审" if overall_passed else "未过审"
        result["违规原因"] = violations
        result["审核备注"] = "已过审" if overall_passed else audit_notes
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
    result["AI审核"] = "跳过审核"
    result["AI审核状态（内部）"] = "跳过审核"
    result["违规原因"] = ""
    result["审核备注"] = "内容为空，跳过审核"
    result["处理时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return result


def _create_existing_review_skip_result(row: Dict[str, Any]) -> Dict[str, Any]:
    """对已审核稿件直接跳过，并禁止写回覆盖原状态。"""
    result = row.copy()
    result["稿件内容"] = ""
    result["AI审核"] = _normalize_audit_status(row.get("AI审核状态（内部）") or row.get("AI审核") or "已审核")
    result["AI审核状态（内部）"] = _normalize_audit_status(row.get("AI审核状态（内部）") or row.get("AI审核") or "已审核")
    result["违规原因"] = ""
    result["审核备注"] = "已审核：跳过"
    result["处理时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
    result["_skip_writeback"] = True
    return result


def _create_error_result(row: Dict[str, Any], error_msg: str) -> Dict[str, Any]:
    """创建错误情况的审核结果"""
    result = row.copy()
    result["稿件内容"] = ""
    result["AI审核"] = "未过审"
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


def _find_header_index(headers: List[str], exact_candidates: List[str], fuzzy_candidates: List[str]) -> Optional[int]:
    """按优先级查找目标列索引，优先精确匹配。"""
    for candidate in exact_candidates:
        if candidate in headers:
            return headers.index(candidate)

    for i, header in enumerate(headers):
        if any(candidate in header for candidate in fuzzy_candidates):
            return i

    return None


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

        ai_audit_internal_col = _find_header_index(headers, ["AI审核状态（内部）"], ["AI审核状态（内部）"])
        ai_audit_col = _find_header_index(headers, ["AI审核", "审核结果"], ["AI审核", "审核结果"])
        violation_col = _find_header_index(headers, ["违规原因"], ["违规", "原因"])
        notes_col = _find_header_index(headers, ["审核备注"], ["备注", "说明"])

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
                status_text = _normalize_audit_status(result.get("AI审核", "")) or "未知状态"

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

        status_text = _normalize_audit_status(result.get("AI审核", "")) or "未知状态"

        fields = {"AI审核状态（内部）": status_text}
        if "审核备注" in headers and result.get("审核备注"):
            fields["审核备注"] = result.get("审核备注", "")
        if "违规原因" in headers and result.get("违规原因"):
            fields["违规原因"] = result.get("违规原因", "")

        updates.append({
            "record_id": record_id,
            "fields": fields
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


def _resolve_review_link_url(row: Dict[str, Any], task_data: Dict[str, Any], content_link: str) -> str:
    """Resolve the best review link URL for the current row."""
    if task_data["type"] == "excel_upload":
        hyperlinks = task_data["data"].get("hyperlinks", {})
        hyperlink_url = get_hyperlink_for_cell(
            hyperlinks,
            row.get("_row_index", 1),
            "稿件链接",
            task_data["data"]["headers"],
        )
        if hyperlink_url:
            return hyperlink_url
    return content_link


def _is_already_reviewed(row: Dict[str, Any]) -> bool:
    """判断稿件是否已过审，已过审才跳过。"""
    audit_values = [
        _cell_to_plain_text(row.get("AI审核状态（内部）")).strip(),
        _cell_to_plain_text(row.get("AI审核")).strip(),
        _cell_to_plain_text(row.get("审核结果")).strip(),
    ]
    for value in audit_values:
        if _normalize_audit_status(value) == "已过审":
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


def _load_image_blocks_for_llm(image_paths: List[str], max_images: int = 4) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for image_path in image_paths[:max_images]:
        try:
            if not image_path or not os.path.exists(image_path):
                continue
            mime_type, _ = mimetypes.guess_type(image_path)
            if mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                continue
            with open(image_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("ascii")
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": encoded,
                },
            })
        except Exception as e:
            logger.warning(f"Failed to prepare image for LLM slogan recheck: {image_path}: {e}")
    return blocks


def _llm_recheck_slogan(client, model: str, content: str, slogan_word: str,
                        image_paths: List[str]) -> Dict[str, Any]:
    """规则和 OCR 都未命中时，使用 LLM 对文本/图片再确认一次口令词。"""
    slogan_word = (slogan_word or "").strip()
    if not client or not model or not slogan_word:
        return {"matched": False, "analysis": ""}

    prompt = f"""你是一个严格的内容审核员。请判断“要求口令词”是否真实出现在稿件正文或图片中。

要求口令词：
{slogan_word}

补充上下文（正文 + OCR）：
{content[:6000] if content else "（空）"}

判断要求：
1. 只判断这个口令词是否出现，不要审别的内容。
2. 优先精确匹配；允许图片里出现轻微分隔符、空格、换行。
3. 如果只是语义接近、不是同一个口令词，不算匹配。
4. 如果无法确认，就返回 matched=false。

请只返回 JSON：
{{
  "matched": true/false,
  "analysis": "简短说明"
}}"""

    content_blocks: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    content_blocks.extend(_load_image_blocks_for_llm(image_paths))

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": content_blocks}],
        )
        result_text = msg.content[0].text.strip()
        if result_text.startswith("```json"):
            result_text = result_text.replace("```json", "").replace("```", "").strip()
        elif result_text.startswith("```"):
            result_text = result_text.replace("```", "").strip()

        result = json.loads(result_text)
        return {
            "matched": bool(result.get("matched")),
            "analysis": str(result.get("analysis") or "").strip(),
        }
    except Exception as e:
        logger.warning(f"LLM slogan recheck failed: {e}")
        return {"matched": False, "analysis": str(e)}


def _llm_recheck_benefits(client, model: str, content: str, missing_benefits: List[str]) -> Dict[str, Any]:
    """仅在规则未匹配到利益点时，使用 LLM 做语义复核。"""
    if not client or not model or not content.strip() or not missing_benefits:
        return {"compliant": False, "matched_benefits": [], "analysis": ""}

    prompt = f"""你是一个严格但理解自然语言变体的审核员。请判断下面稿件是否已经表达了要求的利益点。

稿件全文：
{content}

待确认的利益点标准：
{chr(10).join([f"- {item}" for item in missing_benefits])}

判断要求：
1. 只判断“利益点是否已经表达”，不要审别的内容。
2. 允许谐音、emoji、口语化、省略写法、近义表达。
3. 只有在用户普通理解下能明确对应到该利益点时，才算匹配。
4. 不要因为项目名或标签相近就误判为匹配。

请只返回 JSON：
{{
  "compliant": true/false,
  "matched_benefits": ["已确认命中的利益点标准"],
  "analysis": "简短说明"
}}"""

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = msg.content[0].text.strip()
        if result_text.startswith("```json"):
            result_text = result_text.replace("```json", "").replace("```", "").strip()
        elif result_text.startswith("```"):
            result_text = result_text.replace("```", "").strip()

        result = json.loads(result_text)
        matched = result.get("matched_benefits") or []
        if not isinstance(matched, list):
            matched = [str(matched)] if matched else []
        return {
            "compliant": bool(result.get("compliant")),
            "matched_benefits": [str(item).strip() for item in matched if str(item).strip()],
            "analysis": str(result.get("analysis") or "").strip(),
        }
    except Exception as e:
        logger.warning(f"LLM benefit recheck failed: {e}")
        return {"compliant": False, "matched_benefits": [], "analysis": str(e)}


def _audit_row_against_project(content: str, slogan_word: str, project_name: str,
                               project_config: Dict[str, Any] = None,
                               client=None,
                               model: str = "",
                               image_paths: Optional[List[str]] = None) -> tuple[bool, str, str]:
    """按项目名称和 CSV 规则做确定性审核。"""
    violations = []
    missing_benefits = []
    notes_parts: List[str] = []

    if not project_name:
        violations.append("项目名称为空，无法匹配审核标准")
    if not project_config:
        violations.append(f"未在CSV中找到项目“{project_name or '未知项目'}”对应的审核标准")

    if not violations:
        if slogan_word and not _contains_required_text(content, slogan_word):
            slogan_llm_result = _llm_recheck_slogan(
                client,
                model,
                content,
                slogan_word,
                image_paths or [],
            )
            if slogan_llm_result.get("matched"):
                notes_parts.append(f"LLM复核口令词通过: {slogan_word}")
            else:
                violations.append(f"口令词不一致，正文/图片未确认包含“{slogan_word}”")
                if slogan_llm_result.get("analysis"):
                    notes_parts.append(f"LLM口令词复核说明: {slogan_llm_result['analysis']}")

        for hashtag in _extract_hashtags(project_config.get("话题标签", "")):
            if not _contains_required_text(content, hashtag):
                violations.append(f"缺少话题标签：{hashtag}")

        for benefit in _split_benefit_requirements(project_config.get("利益点标准", "")):
            if not _benefit_requirement_matched(content, benefit):
                missing_benefits.append(benefit)
                violations.append(f"缺少利益点标准：{benefit}")

        if missing_benefits:
            llm_result = _llm_recheck_benefits(client, model, content, missing_benefits)
            matched_benefits = set(llm_result.get("matched_benefits") or [])
            if matched_benefits:
                violations = [
                    item for item in violations
                    if not (
                        item.startswith("缺少利益点标准：")
                        and item.split("：", 1)[1].strip() in matched_benefits
                    )
                ]
                notes_parts.append(
                    "LLM复核利益点通过: " + "；".join(sorted(matched_benefits))
                )
            if llm_result.get("analysis"):
                notes_parts.append(f"LLM利益点复核说明: {llm_result['analysis']}")

    passed = len(violations) == 0
    if passed:
        notes_parts.insert(0, f"按项目“{project_name or '未知项目'}”审核通过")
    else:
        notes_parts.insert(0, "；".join(violations))
    notes = " | ".join([part for part in notes_parts if part])
    return passed, "；".join(violations), notes


def _append_image_audit_notes(base_notes: str, image_paths: List[str], image_text: str,
                              image_fetch_errors: List[str], ocr_result: Dict[str, Any]) -> str:
    notes = [base_notes] if base_notes else []
    ocr_errors = ocr_result.get("errors") or []
    ocr_skip_reason = str(ocr_result.get("skip_reason") or "").strip()
    ocr_available = bool(ocr_result.get("available", True))

    if image_paths:
        notes.append(f"图片审核: 共提取 {len(image_paths)} 张图片")
    if image_text:
        notes.append("图片审核: 已识别图片文字并纳入口令词/利益点/话题标签校验")
    elif image_paths and ocr_skip_reason:
        notes.append(f"图片审核: OCR已跳过（{ocr_skip_reason}），正文审核照常执行")
    elif image_paths and ocr_available:
        notes.append("图片审核: 已处理图片，但未识别到可用文字")
    elif image_paths and not ocr_available:
        notes.append("图片审核: OCR不可用，已跳过图片文字识别，正文审核照常执行")

    if image_fetch_errors:
        notes.append(f"图片下载异常: {'；'.join(image_fetch_errors[:3])}")
    if ocr_errors and not ocr_skip_reason:
        notes.append(f"图片OCR异常: {'；'.join(ocr_errors[:3])}")
    return " | ".join([item for item in notes if item])


def _format_result_item(result: Dict[str, Any], seq: int) -> Dict[str, Any]:
    """将内部结果转换为前端展示结构。"""
    audit_state = _normalize_audit_status(result.get("AI审核", ""))
    skipped = audit_state == "跳过审核"
    if skipped:
        label = "跳过"
    elif audit_state == "已过审":
        label = "已过审"
    elif audit_state == "未过审":
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
