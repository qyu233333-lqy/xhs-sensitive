#!/usr/bin/env python3
"""内容审核 Agent - Web 版 v4 (重构版)"""

import os
import logging
from flask import Flask

from routes.main import main_bp
from routes.api import api_bp

# 应用配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
RESULT_DIR = os.path.join(BASE_DIR, "results")

# 确保目录存在
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULT_DIR, exist_ok=True)


def setup_logging():
    """配置应用日志"""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(BASE_DIR, 'app.log'))
        ]
    )
    return logging.getLogger(__name__)


def create_app():
    """应用工厂函数"""
    app = Flask(__name__)

    # 注册蓝图
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)

    return app


if __name__ == "__main__":
    # 设置日志
    logger = setup_logging()

    # 创建应用
    app = create_app()

    # 运行应用
    logger.info("Starting Content Review Web Application")
    app.run(host="0.0.0.0", port=8000, debug=False)