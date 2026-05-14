"""API端点路由蓝图"""

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, Response, redirect, session

from core.auth import (
    build_callback_redirect,
    build_dingtalk_login_url,
    clear_authenticated_user,
    effective_profile_id,
    exchange_code_for_user_token,
    fetch_dingtalk_user_info,
    get_auth_status,
    get_current_user,
    is_auth_enabled,
    is_auth_ready,
    login_required,
    resolve_user_access,
    store_authenticated_user,
)
from core.config import get_key_profiles_metadata, get_safe_auth_metadata, load_config, save_config
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


def _normalize_status_text(value: object) -> str:
    """把飞书里各种状态文案压成可比较的文本。"""
    text = str(value or "").strip()
    return re.sub(r'[\s\u3000“”"\'：:，,。！？!?.、—\-（）()【】\[\]]+', '', text)


def _is_approved_review_status(value: object) -> bool:
    """识别“已通过/审核通过/通过/已过审”等多种通过状态。"""
    normalized = _normalize_status_text(value)
    if not normalized:
        return False

    negative_markers = ("未通过", "不通过", "未过审", "驳回", "待审核", "未审核")
    if any(marker in normalized for marker in negative_markers):
        return False

    positive_markers = ("审核通过", "已通过", "已过审", "通过")
    return any(marker in normalized for marker in positive_markers)


def _write_bitable_updates_in_chunks(app_token: str, table_id: str, app_id: str, app_secret: str,
                                     updates: list[dict], chunk_size: int = 100) -> bool:
    """分批写回，降低大表单次请求失败概率。"""
    if not updates:
        return True

    for start in range(0, len(updates), chunk_size):
        chunk = updates[start:start + chunk_size]
        success = write_bitable_records(app_token, table_id, app_id, app_secret, chunk)
        if not success:
            return False
    return True


def _run_fill_approved_content(task_id: str) -> None:
    task_data = _tasks[task_id]
    fill_job = task_data.setdefault("fill_job", {})

    try:
        sheet_data = task_data["data"]
        headers = set(sheet_data.get("headers", []))
        config = load_config()
        rows = sheet_data.get("data", [])
        total_rows = len(rows)

        fill_job.update({
            "status": "running",
            "total": total_rows,
            "processed_rows": 0,
            "updated": 0,
            "skipped": 0,
            "message": "开始回填通过稿件",
            "error": "",
        })

        updates = []
        processed = 0
        skipped = 0

        for idx, row in enumerate(rows, 1):
            fill_job["processed_rows"] = idx

            review_status = str(row.get("小题审核状态") or "").strip()
            if not _is_approved_review_status(review_status):
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
            fill_job["updated"] = processed
            fill_job["skipped"] = skipped
            fill_job["message"] = f"正在回填 {idx}/{total_rows}"

        if not updates:
            fill_job.update({
                "status": "completed",
                "updated": 0,
                "skipped": skipped,
                "message": "没有找到可回填的已审核通过稿件",
            })
            return

        success = _write_bitable_updates_in_chunks(
            sheet_data.get("app_token") or sheet_data.get("spreadsheet_id"),
            sheet_data["sheet_id"],
            config["feishu_app_id"],
            config["feishu_app_secret"],
            updates,
        )
        if not success:
            raise ValueError("回填飞书多维表格失败")

        fill_job.update({
            "status": "completed",
            "updated": processed,
            "skipped": skipped,
            "message": f"已回填 {processed} 条稿件内容",
        })
    except Exception as e:
        logger.error(f"Failed to fill approved content for task {task_id}: {e}")
        fill_job.update({
            "status": "failed",
            "error": str(e),
            "message": f"回填失败: {str(e)}",
        })


@api_bp.route("/config", methods=["GET", "POST"])
def handle_config():
    """配置管理端点"""
    try:
        config = load_config()
        auth_enabled = is_auth_enabled(config)
        current_user = get_current_user()
        if auth_enabled and request.method == "POST":
            if not current_user:
                return safe_json_response({"error": "请先使用钉钉登录", "auth_required": True}, 401)
            if not current_user.get("is_admin"):
                return safe_json_response({"error": "需要管理员权限", "auth_required": True}, 403)

        if request.method == "GET":
            profiles = config.get("key_profiles") or {}
            has_any_profile_key = any(
                isinstance(profile, dict) and profile.get("api_key")
                for profile in profiles.values()
            )
            # 隐藏敏感信息
            safe_config = {k: v for k, v in config.items() if k not in ["api_key", "feishu_app_secret", "session_secret"]}
            safe_config["has_api_key"] = bool(config.get("api_key")) or has_any_profile_key
            safe_config["has_feishu_secret"] = bool(config.get("feishu_app_secret"))
            safe_config["key_profiles"] = get_key_profiles_metadata(config)
            safe_config["default_profile_id"] = config.get("default_profile_id", "ops1")
            safe_config["auth"] = get_safe_auth_metadata(config)
            return safe_json_response(safe_config)

        elif request.method == "POST":
            data = request.get_json()
            if not data:
                return safe_json_response({"error": "No data provided"}, 400)

            # 更新配置
            for key, value in data.items():
                if key in ["api_key", "base_url", "model", "feishu_app_id", "feishu_app_secret",
                          "project_config_path", "project_config_feishu_url", "enable_project_audit", "session_secret"]:
                    config[key] = value
                elif key == "auth" and isinstance(value, dict):
                    auth_config = config.get("auth") or {}
                    for auth_key in [
                        "enabled", "dingtalk_app_key", "dingtalk_app_secret",
                        "dingtalk_redirect_uri", "dingtalk_scope", "user_mapping_path"
                    ]:
                        if auth_key in value:
                            if auth_key == "dingtalk_app_secret" and not value[auth_key]:
                                continue
                            auth_config[auth_key] = value[auth_key]
                    config["auth"] = auth_config

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
            if (
                "project_config_path" in data
                or "project_config_feishu_url" in data
                or "enable_project_audit" in data
            ):
                clear_project_configs_cache()

            return safe_json_response({"success": True, "message": "配置已保存"})

    except Exception as e:
        logger.error(f"Config handling failed: {e}")
        return safe_json_response({"error": f"配置操作失败: {str(e)}"}, 500)


@api_bp.route("/parse-url", methods=["POST"])
@login_required()
def parse_url():
    """解析飞书URL端点"""
    try:
        data = request.get_json()
        if not data or "url" not in data:
            return safe_json_response({"error": "缺少URL参数"}, 400)

        url = data["url"].strip()
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
            "profile_id": effective_profile_id(config),
            "requested_by": get_current_user(),
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
@login_required()
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
            "profile_id": effective_profile_id(load_config()),
            "requested_by": get_current_user(),
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
            config = load_config()
            if is_auth_enabled(config):
                current_user = get_current_user()
                if not current_user:
                    return safe_json_response({"error": "请先使用钉钉登录", "auth_required": True}, 401)
                if not current_user.get("is_admin"):
                    return safe_json_response({"error": "需要管理员权限", "auth_required": True}, 403)

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
@login_required()
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
@login_required()
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

        fill_job = task_data.get("fill_job") or {}
        if fill_job.get("status") == "running":
            return safe_json_response({
                "success": True,
                "async": True,
                "task_id": task_id,
                "status": "running",
                "message": fill_job.get("message") or "回填任务进行中",
            }, 202)

        task_data["fill_job"] = {
            "status": "queued",
            "total": len(sheet_data.get("data", [])),
            "processed_rows": 0,
            "updated": 0,
            "skipped": 0,
            "message": "回填任务已启动",
            "error": "",
            "created_at": datetime.now().isoformat(),
        }
        threading.Thread(target=_run_fill_approved_content, args=(task_id,), daemon=True).start()

        return safe_json_response({
            "success": True,
            "async": True,
            "task_id": task_id,
            "status": "queued",
            "message": "回填任务已启动，请等待处理完成",
        }, 202)

    except Exception as e:
        logger.error(f"Failed to fill approved content for task {task_id}: {e}")
        return safe_json_response({"error": f"回填失败: {str(e)}"}, 500)


@api_bp.route("/fill-approved-content/<task_id>/status", methods=["GET"])
@login_required()
def get_fill_approved_content_status(task_id):
    try:
        if task_id not in _tasks:
            return safe_json_response({"error": "任务不存在"}, 404)

        fill_job = (_tasks[task_id].get("fill_job") or {}).copy()
        if not fill_job:
            return safe_json_response({"error": "回填任务未启动"}, 404)
        fill_job["task_id"] = task_id
        return safe_json_response(fill_job)
    except Exception as e:
        logger.error(f"Failed to get fill task status for {task_id}: {e}")
        return safe_json_response({"error": f"获取回填状态失败: {str(e)}"}, 500)




@api_bp.route("/tasks/<task_id>/status")
@login_required()
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
        if is_auth_enabled(config):
            current_user = get_current_user()
            if not current_user:
                return safe_json_response({"default_profile_id": "", "profiles": []})
            return safe_json_response({
                "default_profile_id": current_user.get("profile_id", ""),
                "profiles": [{
                    "id": current_user.get("profile_id"),
                    "label": current_user.get("profile_label") or current_user.get("profile_id"),
                    "has_key": True,
                    "has_base_url": True,
                    "model": "",
                }],
            })
        return safe_json_response({
            "default_profile_id": config.get("default_profile_id", "ops1"),
            "profiles": get_key_profiles_metadata(config),
        })
    except Exception as e:
        logger.error(f"Failed to get key profiles: {e}")
        return safe_json_response({"error": f"获取部门配置失败: {str(e)}"}, 500)


@api_bp.route("/auth/me", methods=["GET"])
def auth_me():
    try:
        return safe_json_response(get_auth_status(load_config()))
    except Exception as e:
        logger.error(f"Failed to get auth status: {e}")
        return safe_json_response({"error": f"获取登录状态失败: {str(e)}"}, 500)


@api_bp.route("/auth/dingtalk/login", methods=["GET"])
def auth_dingtalk_login():
    try:
        config = load_config()
        if not is_auth_enabled(config):
            return safe_json_response({"error": "钉钉登录未启用"}, 400)
        if not is_auth_ready(config):
            return safe_json_response({"error": "钉钉登录配置不完整"}, 503)
        return redirect(build_dingtalk_login_url(config))
    except Exception as e:
        logger.error(f"Failed to start DingTalk login: {e}")
        return redirect(build_callback_redirect("login_failed"))


@api_bp.route("/auth/dingtalk/callback", methods=["GET"])
def auth_dingtalk_callback():
    try:
        config = load_config()
        if not is_auth_enabled(config):
            return redirect(build_callback_redirect("auth_disabled"))

        code = (request.args.get("code") or "").strip()
        state = (request.args.get("state") or "").strip()
        expected_state = session.pop("dingtalk_oauth_state", "")
        if not code:
            return redirect(build_callback_redirect("missing_code"))
        if expected_state and state != expected_state:
            return redirect(build_callback_redirect("state_mismatch"))

        user_token = exchange_code_for_user_token(code, config)
        user_info = fetch_dingtalk_user_info(user_token, config)
        auth_user = resolve_user_access(user_info, config)
        store_authenticated_user(auth_user)

        logger.info(
            "Authenticated DingTalk user: %s -> %s",
            auth_user.get("display_name"),
            auth_user.get("profile_id"),
        )
        return redirect(build_callback_redirect())
    except PermissionError as e:
        logger.warning(f"DingTalk user denied: {e}")
        clear_authenticated_user()
        return redirect(build_callback_redirect("unauthorized_user"))
    except Exception as e:
        logger.error(f"Failed to complete DingTalk login: {e}")
        clear_authenticated_user()
        return redirect(build_callback_redirect("login_failed"))


@api_bp.route("/auth/logout", methods=["POST"])
def auth_logout():
    clear_authenticated_user()
    return safe_json_response({"success": True})
