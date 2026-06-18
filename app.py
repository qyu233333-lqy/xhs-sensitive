#!/usr/bin/env python3
"""内容审核 Agent - Web 版 v4 (重构版)"""

import os
import logging
from datetime import timedelta
from flask import Flask

from routes.main import main_bp
from routes.api import api_bp
from core.config import load_config

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
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level))

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    stream_handler_exists = any(
        isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        for handler in root_logger.handlers
    )
    file_handler_exists = any(
        isinstance(handler, logging.FileHandler)
        and getattr(handler, "baseFilename", "") == os.path.join(BASE_DIR, 'app.log')
        for handler in root_logger.handlers
    )

    if not stream_handler_exists:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    if not file_handler_exists:
        file_handler = logging.FileHandler(os.path.join(BASE_DIR, 'app.log'))
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return logging.getLogger(__name__)


def create_app():
    """应用工厂函数"""
    setup_logging()
    app = Flask(__name__)
    config = load_config()
    app.secret_key = (
        os.getenv("SESSION_SECRET")
        or config.get("session_secret")
        or "dev-session-secret-change-me"
    )
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )

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
    app.run(host="0.0.0.0", port=8002, debug=False)
