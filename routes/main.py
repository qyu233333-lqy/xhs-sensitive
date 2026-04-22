"""主要Web界面路由蓝图"""

import logging
from flask import Blueprint, render_template, send_file, abort
from core.config import load_config

logger = logging.getLogger(__name__)

# 创建蓝图
main_bp = Blueprint('main', __name__)


@main_bp.route("/")
def index():
    """主页面"""
    try:
        config = load_config()
        # 检查基本配置
        has_config = bool(config.get("api_key"))
        has_feishu = bool(config.get("feishu_app_id") and config.get("feishu_app_secret"))

        return render_template("index.html",
                             has_config=has_config,
                             has_feishu=has_feishu)
    except Exception as e:
        logger.error(f"Failed to load index page: {e}")
        return render_template("index.html",
                             has_config=False,
                             has_feishu=False)


@main_bp.route("/api/download/<task_id>")
def download_results(task_id):
    """下载审核结果文件"""
    import os
    from core.config import load_config

    try:
        config = load_config()
        result_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")

        # 查找匹配的结果文件
        possible_files = [
            f"{task_id}_processed.xlsx",
            f"{task_id}_results.xlsx",
            f"{task_id}.xlsx"
        ]

        for filename in possible_files:
            filepath = os.path.join(result_dir, filename)
            if os.path.exists(filepath):
                logger.info(f"Serving download file: {filename}")
                return send_file(
                    filepath,
                    as_attachment=True,
                    download_name=f"审核结果_{task_id}.xlsx",
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                )

        logger.warning(f"Download file not found for task_id: {task_id}")
        abort(404)

    except Exception as e:
        logger.error(f"Download failed for task_id {task_id}: {e}")
        abort(500)