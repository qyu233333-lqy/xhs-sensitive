"""API端点路由蓝图"""

import json
import logging
import os
import time
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, Response

from core.config import get_key_profiles_metadata, load_config, resolve_ai_profile, save_config
from core.project import clear_project_configs_cache, load_project_configs, save_project_config
from core.feishu import (
    download_feishu_doc_snapshot,
    extract_review_doc_sections,
    fetch_feishu_sheet,
    validate_feishu_config,
    write_bitable_records,
)
from core.file_utils import parse_xlsx, sanitize_filename, is_valid_file_type, get_file_size_mb

logger = logging.getLogger(__name__)

# 创建蓝图
api_bp = Blueprint('api', __name__, url_prefix='/api')

# 全局变量
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
RESULT_DIR = os.path.join(BASE_DIR, "results")

# 确保目录存在
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)

# 任务存储
_tasks = {}


def safe_json_response(data, status=200):
    """创建安全的JSON响应"""
    try:
        return jsonify(data), status
    except (TypeError, ValueError) as e:
        logger.error(f"Failed to create JSON response: {e}")
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route("/config", methods=["GET", "POST"])
def handle_config():
    """配置管理端点"""
    try:
        if request.method == "GET":
            config = load_config()
            profiles = config.get("key_profiles") or {}
            has_any_profile_key = any(
                isinstance(profile, dict) and profile.get("api_key")
                for profile in profiles.values()
            )
            # 隐藏敏感信息
            safe_config = {k: v for k, v in config.items() if k not in ["api_key", "feishu_app_secret"]}
            safe_config["has_api_key"] = bool(config.get("api_key")) or has_any_profile_key
            safe_config["has_feishu_secret"] = bool(config.get("feishu_app_secret"))
            safe_config["key_profiles"] = get_key_profiles_metadata(config)
            safe_config["default_profile_id"] = config.get("default_profile_id", "ops1")
            return safe_json_response(safe_config)

        elif request.method == "POST":
            data = request.get_json()
            if not data:
                return safe_json_response({"error": "No data provided"}, 400)

            config = load_config()

            # 更新配置
            for key, value in data.items():
                if key in ["api_key", "base_url", "model", "feishu_app_id", "feishu_app_secret",
                          "project_config_path", "project_config_feishu_url", "enable_project_audit"]:
                    config[key] = value

            # 验证飞书配置（如果提供）
            if config.get("feishu_app_id") and config.get("feishu_app_secret"):
                is_valid, error_msg = validate_feishu_config(
                    config["feishu_app_id"],
                    config["feishu_app_secret"]
                )
                if not is_valid:
                    return safe_json_response({
                        "error": f"飞书配置验证失败: {error_msg}"
                    }, 400)

            # 保存配置
            save_config(config)

            # 清除项目配置缓存（如果项目配置路径发生变化）
            if "project_config_path" in data or "enable_project_audit" in data:
                clear_project_configs_cache()

            return safe_json_response({"success": True, "message": "配置已保存"})

    except Exception as e:
        logger.error(f"Config handling failed: {e}")
        return safe_json_response({"error": f"配置操作失败: {str(e)}"}, 500)


@api_bp.route("/parse-url", methods=["POST"])
def parse_url():
    """解析飞书URL端点"""
    try:
        data = request.get_json()
        if not data or "url" not in data:
            return safe_json_response({"error": "缺少URL参数"}, 400)

        url = data["url"].strip()
        profile_id = (data.get("profile_id") or "").strip() or None
        if not url:
            return safe_json_response({"error": "URL不能为空"}, 400)

        # 验证URL格式
        if not any(domain in url for domain in ["feishu.cn", "larksuite.com"]):
            return safe_json_response({"error": "不支持的URL格式，请使用飞书表格分享链接"}, 400)

        config = load_config()
        if not config.get("feishu_app_id") or not config.get("feishu_app_secret"):
            return safe_json_response({"error": "请先配置飞书应用信息"}, 400)

        # 解析飞书表格
        sheet_data = fetch_feishu_sheet(
            url,
            config["feishu_app_id"],
            config["feishu_app_secret"]
        )

        # 生成任务ID
        task_id = str(uuid.uuid4())[:8]
        task_data = {
            "task_id": task_id,
            "type": "feishu_url",
            "url": url,
            "profile_id": profile_id or config.get("default_profile_id") or "ops1",
            "data": sheet_data,
            "status": "ready",
            "created_at": datetime.now().isoformat()
        }
        _tasks[task_id] = task_data

        response_data = {
            "task_id": task_id,
            "type": "feishu",
            "sheet": sheet_data["title"],
            "title": sheet_data["title"],
            "total": sheet_data["total_rows"],
            "new_count": sheet_data["total_rows"],
            "total_rows": sheet_data["total_rows"],
            "headers": sheet_data["headers"]
        }

        logger.info(f"Successfully parsed Feishu URL: {sheet_data['total_rows']} rows")
        return safe_json_response(response_data)

    except Exception as e:
        logger.error(f"URL parsing failed: {e}")
        return safe_json_response({"error": f"解析失败: {str(e)}"}, 500)


@api_bp.route("/upload", methods=["POST"])
def upload_file():
    """文件上传端点"""
    try:
        if "file" not in request.files:
            return safe_json_response({"error": "没有文件被上传"}, 400)

        file = request.files["file"]
        if file.filename == "":
            return safe_json_response({"error": "未选择文件"}, 400)

        # 验证文件类型
        if not is_valid_file_type(file.filename):
            return safe_json_response({"error": "不支持的文件类型，请上传Excel文件(.xlsx/.xls)"}, 400)

        # 生成安全的文件名
        original_filename = file.filename
        safe_filename = sanitize_filename(original_filename)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        saved_filename = f"{timestamp}_{safe_filename}"
        file_path = os.path.join(UPLOAD_DIR, saved_filename)

        # 保存文件
        file.save(file_path)

        # 检查文件大小
        file_size = get_file_size_mb(file_path)
        if file_size > 50:  # 50MB限制
            os.remove(file_path)
            return safe_json_response({"error": "文件过大，请上传小于50MB的文件"}, 400)

        # 解析Excel文件
        excel_data = parse_xlsx(file_path)

        # 生成任务ID
        task_id = str(uuid.uuid4())[:8]
        task_data = {
            "task_id": task_id,
            "type": "excel_upload",
            "profile_id": request.form.get("profile_id") or load_config().get("default_profile_id") or "ops1",
            "file_path": file_path,
            "original_filename": original_filename,
            "data": excel_data,
            "status": "ready",
            "created_at": datetime.now().isoformat()
        }
        _tasks[task_id] = task_data

        response_data = {
            "task_id": task_id,
            "type": "excel",
            "filename": original_filename,
            "file_size_mb": file_size,
            "total_rows": excel_data["total_rows"],
            "headers": excel_data["headers"]
        }

        logger.info(f"Successfully uploaded and parsed Excel: {excel_data['total_rows']} rows")
        return safe_json_response(response_data)

    except Exception as e:
        logger.error(f"File upload failed: {e}")
        return safe_json_response({"error": f"上传失败: {str(e)}"}, 500)


@api_bp.route("/project-configs", methods=["GET", "POST"])
def get_project_configs():
    """获取项目配置列表"""
    try:
        if request.method == "POST":
            data = request.get_json() or {}
            saved = save_project_config({
                "项目名称": data.get("project_name", ""),
                "项目介绍": data.get("description", ""),
                "话题标签": data.get("hashtags", ""),
                "利益点标准": data.get("benefit_standards", ""),
                "口令要求": data.get("slogan_requirements", ""),
                "审核严格度": data.get("audit_strictness", "normal"),
            })
            return safe_json_response({
                "success": True,
                "message": f"已{'更新' if saved['action'] == 'updated' else '新增'}审核标准：{saved['project_name']}",
                **saved,
            })

        configs = load_project_configs()

        # 转换为前端友好的格式
        config_list = []
        for project_name, config in configs.items():
            config_list.append({
                "project_name": project_name,
                "description": config.get('项目介绍', ''),
                "hashtags": config.get('话题标签', ''),
                "benefit_standards": config.get('利益点标准', ''),
                "slogan_requirements": config.get('口令要求', ''),
                "audit_strictness": config.get('审核严格度', 'normal')
            })

        return safe_json_response({
            "total": len(config_list),
            "configs": config_list
        })

    except FileNotFoundError:
        return safe_json_response({
            "error": "项目配置文件未找到，请检查 project_config_path 设置"
        }, 404)
    except Exception as e:
        logger.error(f"Failed to get project configs: {e}")
        return safe_json_response({
            "error": f"获取项目配置失败: {str(e)}"
        }, 500)


@api_bp.route("/project-configs/reload", methods=["POST"])
def reload_project_configs():
    """重新加载项目配置"""
    try:
        clear_project_configs_cache()
        configs = load_project_configs()

        return safe_json_response({
            "success": True,
            "message": f"已重新加载 {len(configs)} 个项目配置"
        })

    except Exception as e:
        logger.error(f"Failed to reload project configs: {e}")
        return safe_json_response({
            "error": f"重新加载项目配置失败: {str(e)}"
        }, 500)


@api_bp.route("/review/<task_id>")
def start_review(task_id):
    """启动审核流程（SSE流）"""
    from core.review_engine import process_review_task

    try:
        if task_id not in _tasks:
            return safe_json_response({"error": "任务不存在"}, 404)

        task_data = _tasks[task_id]
        if task_data["status"] != "ready":
            return safe_json_response({"error": "任务状态无效"}, 400)

        # 更新任务状态
        task_data["status"] = "processing"

        def generate_progress():
            try:
                # 启动审核流程
                for progress_data in process_review_task(task_data):
                    yield f"data: {json.dumps(progress_data, ensure_ascii=False)}\n\n"

                task_data["status"] = "completed"

            except Exception as e:
                logger.error(f"Review process failed for task {task_id}: {e}")
                task_data["status"] = "failed"
                task_data["error"] = str(e)
                yield f"data: {json.dumps({'type': 'error', 'msg': str(e)}, ensure_ascii=False)}\n\n"

        return Response(generate_progress(), mimetype='text/event-stream')

    except Exception as e:
        logger.error(f"Failed to start review for task {task_id}: {e}")
        return safe_json_response({"error": f"启动审核失败: {str(e)}"}, 500)


@api_bp.route("/fill-approved-content/<task_id>", methods=["POST"])
def fill_approved_content(task_id):
    """将已通过小题审核的稿件链接内容回填到表格列。"""
    try:
        if task_id not in _tasks:
            return safe_json_response({"error": "任务不存在"}, 404)

        task_data = _tasks[task_id]
        if task_data.get("type") != "feishu_url":
            return safe_json_response({"error": "仅支持飞书表格任务"}, 400)

        sheet_data = task_data["data"]
        if not sheet_data.get("is_bitable"):
            return safe_json_response({"error": "当前仅支持飞书多维表格回填"}, 400)

        headers = set(sheet_data.get("headers", []))
        required_headers = {"标题", "文案", "评论区文案", "小题审核状态", "稿件链接"}
        missing = [header for header in required_headers if header not in headers]
        if missing:
            return safe_json_response({"error": f"当前表格缺少必需列: {', '.join(missing)}"}, 400)

        config = load_config()
        if not config.get("feishu_app_id") or not config.get("feishu_app_secret"):
            return safe_json_response({"error": "请先配置飞书应用信息"}, 400)

        updates = []
        processed = 0
        skipped = 0

        for row in sheet_data.get("data", []):
            review_status = str(row.get("小题审核状态") or "").strip()
            if "审核通过" not in review_status:
                skipped += 1
                continue

            record_id = row.get("_record_id")
            link_value = row.get("稿件链接")
            snapshot = download_feishu_doc_snapshot(
                link_value,
                config["feishu_app_id"],
                config["feishu_app_secret"],
            )
            content = snapshot.get("content", "").strip()
            if not record_id or not content:
                skipped += 1
                continue

            sections = extract_review_doc_sections(content)
            fields = {}
            for field_name in ("标题", "文案", "评论区文案"):
                value = str(sections.get(field_name) or "").strip()
                if value:
                    fields[field_name] = value

            if not fields:
                skipped += 1
                continue

            updates.append({"record_id": record_id, "fields": fields})
            processed += 1

        if not updates:
            return safe_json_response({
                "success": True,
                "updated": 0,
                "skipped": skipped,
                "message": "没有找到可回填的已审核通过稿件"
            })

        success = write_bitable_records(
            sheet_data.get("app_token") or sheet_data.get("spreadsheet_id"),
            sheet_data["sheet_id"],
            config["feishu_app_id"],
            config["feishu_app_secret"],
            updates,
        )
        if not success:
            return safe_json_response({"error": "回填飞书多维表格失败"}, 500)

        return safe_json_response({
            "success": True,
            "updated": processed,
            "skipped": skipped,
            "message": f"已回填 {processed} 条稿件内容"
        })

    except Exception as e:
        logger.error(f"Failed to fill approved content for task {task_id}: {e}")
        return safe_json_response({"error": f"回填失败: {str(e)}"}, 500)




@api_bp.route("/tasks/<task_id>/status")
def get_task_status(task_id):
    """获取任务状态"""
    try:
        if task_id not in _tasks:
            return safe_json_response({"error": "任务不存在"}, 404)

        task_data = _tasks[task_id]
        status_data = {
            "task_id": task_id,
            "status": task_data["status"],
            "created_at": task_data["created_at"]
        }

        if "error" in task_data:
            status_data["error"] = task_data["error"]

        return safe_json_response(status_data)

    except Exception as e:
        logger.error(f"Failed to get task status for {task_id}: {e}")
        return safe_json_response({"error": f"获取任务状态失败: {str(e)}"}, 500)


@api_bp.route("/key-profiles", methods=["GET"])
def get_key_profiles():
    """获取可供前端选择的部门配置，不返回真实密钥。"""
    try:
        config = load_config()
        return safe_json_response({
            "default_profile_id": config.get("default_profile_id", "ops1"),
            "profiles": get_key_profiles_metadata(config),
        })
    except Exception as e:
        logger.error(f"Failed to get key profiles: {e}")
        return safe_json_response({"error": f"获取部门配置失败: {str(e)}"}, 500)
