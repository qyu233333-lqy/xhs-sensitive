"""LLM内容元素提取核心模块"""

import json
import re
import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


def extract_content_elements(client, model: str, content: str) -> Dict[str, Any]:
    """使用LLM提取稿件的结构化信息

    Args:
        client: Anthropic client实例
        model: 使用的模型名称
        content: 稿件内容文本

    Returns:
        dict: 结构化的内容信息
    """
    if not content or not content.strip():
        logger.warning("Content is empty for extraction")
        return {
            "hashtags": [],
            "slogans": [],
            "benefits": [],
            "brands": [],
            "title": "",
            "main_content": ""
        }

    extraction_prompt = f"""你是一个专业的内容分析师。请从以下稿件中提取关键信息，以JSON格式输出：

稿件内容：
{content}

请提取以下信息（如果不存在则为空数组/空字符串）：

1. 话题标签：所有#开头的标签，以及任何看起来像话题标签的内容（包括变体形式）
2. 口令/优惠码：任何看起来像优惠码、活动口令、兑换码、促销码的内容
3. 利益点/权益描述：任何描述优惠、权益、福利、特色服务的具体内容
4. 品牌提及：提到的品牌名称、商家名称、产品名称
5. 标题：稿件的标题或主要标题
6. 核心文案：去除标签后的主要文字内容

输出JSON格式：
{{
    "hashtags": ["#标签1", "#标签2"],
    "slogans": ["口令1", "优惠码2"],
    "benefits": ["具体权益描述1", "利益点2"],
    "brands": ["品牌1", "品牌2"],
    "title": "标题内容",
    "main_content": "核心文案内容"
}}

提取要求：
- 话题标签：包含所有可能的标签，不只是#开头的，还包括可能的同义词和变体
- 口令/优惠码：包括任何数字字母组合、活动代码、兑换码等
- 利益点：详细提取，包括优惠幅度、权益内容、使用条件等
- 品牌：包括主品牌、子品牌、产品名称等
- 宁可多提取，不要遗漏重要信息
- 确保JSON格式正确，不要包含注释"""

    try:
        logger.debug("Extracting content elements using LLM")

        msg = client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": extraction_prompt
            }]
        )

        result_text = msg.content[0].text.strip()
        logger.debug(f"LLM extraction raw result: {result_text[:200]}...")

        # 清理可能的markdown格式
        if result_text.startswith('```json'):
            result_text = result_text.replace('```json', '').replace('```', '').strip()
        elif result_text.startswith('```'):
            result_text = result_text.replace('```', '').strip()

        extracted = json.loads(result_text)

        # 验证返回的数据结构
        required_keys = ["hashtags", "slogans", "benefits", "brands", "title", "main_content"]
        for key in required_keys:
            if key not in extracted:
                logger.warning(f"Missing key '{key}' in extraction result, setting to default")
                if key in ["hashtags", "slogans", "benefits", "brands"]:
                    extracted[key] = []
                else:
                    extracted[key] = ""

        # 确保数组字段确实是数组
        array_fields = ["hashtags", "slogans", "benefits", "brands"]
        for field in array_fields:
            if not isinstance(extracted[field], list):
                logger.warning(f"Field '{field}' is not a list, converting")
                extracted[field] = [str(extracted[field])] if extracted[field] else []

        # 确保字符串字段确实是字符串
        string_fields = ["title", "main_content"]
        for field in string_fields:
            if not isinstance(extracted[field], str):
                logger.warning(f"Field '{field}' is not a string, converting")
                extracted[field] = str(extracted[field]) if extracted[field] else ""

        logger.debug(f"Successfully extracted {len(extracted['hashtags'])} hashtags, "
                    f"{len(extracted['benefits'])} benefits, {len(extracted['slogans'])} slogans")

        return extracted

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM extraction result as JSON: {e}")
        logger.error(f"Raw result: {result_text}")
        # 降级：返回空结构但包含原内容
        return {
            "hashtags": [],
            "slogans": [],
            "benefits": [],
            "brands": [],
            "title": "",
            "main_content": content
        }
    except Exception as e:
        logger.error(f"LLM content extraction failed: {e}")
        # 降级：返回空结构但包含原内容
        return {
            "hashtags": [],
            "slogans": [],
            "benefits": [],
            "brands": [],
            "title": "",
            "main_content": content
        }


def extract_content_with_fallback(client, model: str, content: str) -> Dict[str, Any]:
    """带降级机制的内容提取

    首先尝试LLM提取，失败时使用简单的正则表达式作为后备方案
    """
    try:
        # 首先尝试LLM提取
        return extract_content_elements(client, model, content)
    except Exception as e:
        logger.warning(f"LLM extraction failed, falling back to regex: {e}")

        # 降级到简单的正则提取
        hashtags = re.findall(r'#[\u4e00-\u9fa5\w]+', content)

        # 简单的口令提取（数字字母组合）
        slogans = re.findall(r'\b[A-Z0-9]{4,}\b', content)

        # 简单的利益点提取（包含常见关键词的句子）
        benefit_keywords = ['折', '免费', '优惠', '赠送', '抵扣', '减', '权益', '会员']
        benefits = []
        for sentence in re.split(r'[。！？\n]', content):
            if any(keyword in sentence for keyword in benefit_keywords):
                benefits.append(sentence.strip())

        return {
            "hashtags": hashtags,
            "slogans": slogans,
            "benefits": benefits,
            "brands": [],  # 正则难以准确识别品牌
            "title": content.split('\n')[0] if '\n' in content else "",
            "main_content": content
        }