#!/usr/bin/env python3
"""测试飞书链接读取功能"""

import sys
import os

# 添加项目路径到系统路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.feishu import fetch_feishu_content
from core.config import load_config

def test_feishu_link():
    """测试飞书链接读取"""
    # 用户提供的链接
    url = "https://my.feishu.cn/wiki/JDUzwT1iliRNDfk5YsAckiTSnze"

    # 加载配置
    config = load_config()
    app_id = config.get("feishu_app_id")
    app_secret = config.get("feishu_app_secret")

    if not app_id or not app_secret:
        print("❌ 飞书配置缺失")
        return False

    print(f"🔍 正在读取飞书链接: {url}")
    print(f"📱 使用应用ID: {app_id}")

    try:
        content = fetch_feishu_content(url, app_id, app_secret)

        if content:
            print(f"✅ 成功读取内容！")
            print(f"📄 内容长度: {len(content)} 字符")
            print("📖 内容预览:")
            print("-" * 50)
            print(content[:500])  # 显示前500字符
            if len(content) > 500:
                print("...")
            print("-" * 50)
            return True
        else:
            print("❌ 无法读取内容")
            return False

    except Exception as e:
        print(f"❌ 读取失败: {e}")
        return False

if __name__ == "__main__":
    test_feishu_link()