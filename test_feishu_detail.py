#!/usr/bin/env python3
"""详细测试飞书链接读取功能"""

import sys
import os
import json

# 添加项目路径到系统路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.feishu import get_feishu_token
from core.config import load_config
import requests

def test_feishu_link_detailed():
    """详细测试飞书链接读取"""
    # 用户提供的链接
    url = "https://my.feishu.cn/wiki/JDUzwT1iliRNDfk5YsAckiTSnze"

    # 加载配置
    config = load_config()
    app_id = config.get("feishu_app_id")
    app_secret = config.get("feishu_app_secret")

    if not app_id or not app_secret:
        print("❌ 飞书配置缺失")
        return False

    print(f"🔍 正在详细测试飞书链接: {url}")
    print(f"📱 使用应用ID: {app_id}")

    # 从URL中提取文档ID
    doc_id = url.split('/wiki/')[1].split('?')[0].split('#')[0]
    print(f"📄 文档ID: {doc_id}")

    try:
        # 获取访问令牌
        token = get_feishu_token(app_id, app_secret)
        if not token:
            print("❌ 无法获取访问令牌")
            return False

        print(f"🔑 成功获取访问令牌: {token[:20]}...")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        # 步骤1: 获取wiki节点信息
        print("\n📋 步骤1: 获取wiki节点信息")
        api_url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node"
        params = {"token": doc_id}

        response = requests.get(api_url, headers=headers, params=params, timeout=15)
        print(f"HTTP状态: {response.status_code}")

        if response.status_code != 200:
            print(f"❌ Wiki API错误: HTTP {response.status_code}")
            print(f"响应内容: {response.text}")
            return False

        result = response.json()
        print(f"API响应: {json.dumps(result, indent=2, ensure_ascii=False)}")

        if result.get("code") != 0:
            print(f"❌ Wiki API错误: {result.get('msg', 'Unknown error')}")
            return False

        node_data = result.get("data", {}).get("node", {})
        title = node_data.get("title", "")
        obj_token = node_data.get("obj_token", "")

        print(f"📖 文档标题: {title}")
        print(f"🔗 obj_token: {obj_token}")

        # 步骤2: 如果有obj_token，尝试获取详细内容
        if obj_token:
            print("\n📋 步骤2: 获取详细内容")
            content_url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{obj_token}/content"
            content_response = requests.get(content_url, headers=headers, timeout=15)

            print(f"内容API HTTP状态: {content_response.status_code}")

            if content_response.status_code == 200:
                content_data = content_response.json()
                print(f"内容API响应代码: {content_data.get('code')}")

                if content_data.get("code") == 0:
                    print("✅ 成功获取详细内容!")

                    # 解析文档结构
                    document = content_data.get("data", {}).get("document", {})
                    body = document.get("body", {})
                    blocks = body.get("blocks", [])

                    print(f"📊 文档包含 {len(blocks)} 个内容块")

                    # 提取文本内容
                    full_text = title + "\n"

                    for i, block in enumerate(blocks):
                        block_type = block.get("block_type")
                        print(f"  块 {i+1}: {block_type}")

                        if block_type == "text":
                            text_elements = block.get("text", {}).get("elements", [])
                            for element in text_elements:
                                if element.get("type") == "text_run":
                                    content = element.get("text_run", {}).get("content", "")
                                    full_text += content

                        elif block_type in ["heading1", "heading2", "heading3"]:
                            text_elements = block.get(block_type, {}).get("elements", [])
                            heading_text = ""
                            for element in text_elements:
                                if element.get("type") == "text_run":
                                    content = element.get("text_run", {}).get("content", "")
                                    heading_text += content
                            if heading_text:
                                prefix = "#" * int(block_type[-1])
                                full_text += f"\n{prefix} {heading_text}\n"

                        elif block_type == "bullet_list":
                            list_items = block.get("bullet_list", {}).get("elements", [])
                            for item in list_items:
                                item_text = ""
                                elements = item.get("elements", [])
                                for element in elements:
                                    if element.get("type") == "text_run":
                                        content = element.get("text_run", {}).get("content", "")
                                        item_text += content
                                if item_text:
                                    full_text += f"• {item_text}\n"

                    print(f"\n📖 完整内容 ({len(full_text)} 字符):")
                    print("=" * 60)
                    print(full_text)
                    print("=" * 60)

                    return True
                else:
                    print(f"❌ 内容API错误: {content_data.get('msg', 'Unknown error')}")
            else:
                print(f"❌ 内容API HTTP错误: {content_response.status_code}")
                print(f"响应内容: {content_response.text}")
        else:
            print("⚠️  没有obj_token，只能显示标题")
            print(f"📖 标题内容: {title}")

        return False

    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    test_feishu_link_detailed()