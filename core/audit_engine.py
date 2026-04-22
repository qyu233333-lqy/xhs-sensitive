"""审核引擎核心模块"""

import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

REVIEW_SYSTEM = """你是一名专业的内容审核员。根据审核规则检查稿件是否存在违规。

重要：这些稿件是品牌方授权的商业合作内容（商单），以下不算违规：
- 品牌露出、产品推广、活动宣传、引导领取体验卡等营销行为
- 提及合作方授权的明星/KOL
- 促销信息、推广话术

只关注真正的内容违规：
1. 违禁词或敏感词（政治敏感、暴力、色情等）
2. 违反公序良俗（抽烟酗酒、虐待、歧视、炫富卖惨、脏话等）
3. 违法行为（赌博、毒品、传销等）
4. 低俗色情暗示
5. 危害未成年人
6. 危害公共安全
7. 封建迷信
8. 虚假/伪科学
9. 侵犯隐私（车牌号、地址等）
10. 搬运抄袭（其他平台水印等）"""


def review_one(client, model: str, content: str, rules: str = None) -> Dict[str, Any]:
    """使用LLM进行单个内容审核

    Args:
        client: Anthropic client实例
        model: 使用的模型名称
        content: 审核内容
        rules: 自定义审核规则，为None时使用默认规则

    Returns:
        dict: {"passed": bool, "violations": [str], "notes": str}
    """
    system_msg = rules if rules else REVIEW_SYSTEM

    if not content or not content.strip():
        logger.warning("Content is empty for review")
        return {
            "passed": True,
            "violations": [],
            "notes": "内容为空，跳过审核"
        }

    try:
        logger.debug("Starting content review with LLM")

        msg = client.messages.create(
            model=model,
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": f"请审核以下稿件：\n\n{content}\n\n"
                          f"请以JSON格式返回审核结果：\n"
                          f"{{\n"
                          f'  "passed": true/false,\n'
                          f'  "violations": ["违规类型1", "违规类型2"],\n'
                          f'  "notes": "详细说明"\n'
                          f"}}"
            }]
        )

        result_text = msg.content[0].text.strip()
        logger.debug(f"LLM review raw result: {result_text[:200]}...")

        # 清理可能的markdown格式
        if result_text.startswith('```json'):
            result_text = result_text.replace('```json', '').replace('```', '').strip()
        elif result_text.startswith('```'):
            result_text = result_text.replace('```', '').strip()

        result = json.loads(result_text)

        # 验证返回的数据结构
        if "passed" not in result:
            logger.warning("Missing 'passed' field in review result")
            result["passed"] = True

        if "violations" not in result:
            result["violations"] = []
        elif not isinstance(result["violations"], list):
            result["violations"] = [str(result["violations"])] if result["violations"] else []

        if "notes" not in result:
            result["notes"] = ""
        elif not isinstance(result["notes"], str):
            result["notes"] = str(result["notes"])

        logger.debug(f"Review completed: passed={result['passed']}, violations={len(result['violations'])}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM review result as JSON: {e}")
        logger.error(f"Raw result: {result_text}")
        # 降级：返回通过但带备注
        return {
            "passed": True,
            "violations": [],
            "notes": f"审核结果解析失败，请人工确认。原始回复：{result_text[:200]}..."
        }
    except Exception as e:
        logger.error(f"LLM content review failed: {e}")
        # 降级：返回通过但带备注
        return {
            "passed": True,
            "violations": [],
            "notes": f"审核失败：{str(e)}"
        }


def audit_hashtags_vs_project(client, model: str, content_hashtags: list,
                              project_hashtags: list, strict_mode: bool = True) -> Dict[str, Any]:
    """使用LLM进行话题标签合规性审核

    Args:
        client: Anthropic client实例
        model: 使用的模型名称
        content_hashtags: 内容中提取的话题标签列表
        project_hashtags: 项目要求的话题标签列表
        strict_mode: 严格模式，True时必须包含所有项目标签

    Returns:
        dict: {"compliant": bool, "missing": [str], "extra": [str], "analysis": str}
    """
    if not content_hashtags and not project_hashtags:
        return {
            "compliant": True,
            "missing": [],
            "extra": [],
            "analysis": "无话题标签要求"
        }

    if not project_hashtags:
        return {
            "compliant": True,
            "missing": [],
            "extra": content_hashtags,
            "analysis": f"项目无标签要求，内容包含{len(content_hashtags)}个标签"
        }

    audit_prompt = f"""你是一个专业的内容审核员，负责检查话题标签的合规性。

项目要求的话题标签：{project_hashtags}
内容实际的话题标签：{content_hashtags}

请分析标签合规性，考虑以下因素：
1. 标签的语义相似性（如 #美食 和 #美食探店 可能是相关的）
2. 标签的变体形式（如 #南京大牌档 和 #大牌档 可能是相关的）
3. 审核模式：{"严格模式（必须包含所有要求标签）" if strict_mode else "宽松模式（包含主要标签即可）"}

请以JSON格式返回审核结果：
{{
    "compliant": true/false,
    "missing": ["缺失的要求标签"],
    "extra": ["额外的标签"],
    "analysis": "详细分析说明"
}}"""

    try:
        logger.debug("Starting hashtag compliance audit with LLM")

        msg = client.messages.create(
            model=model,
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": audit_prompt
            }]
        )

        result_text = msg.content[0].text.strip()
        logger.debug(f"LLM hashtag audit raw result: {result_text[:200]}...")

        # 清理markdown格式
        if result_text.startswith('```json'):
            result_text = result_text.replace('```json', '').replace('```', '').strip()
        elif result_text.startswith('```'):
            result_text = result_text.replace('```', '').strip()

        result = json.loads(result_text)

        # 验证返回的数据结构
        required_keys = ["compliant", "missing", "extra", "analysis"]
        for key in required_keys:
            if key not in result:
                logger.warning(f"Missing key '{key}' in hashtag audit result")
                if key == "compliant":
                    result[key] = True
                elif key in ["missing", "extra"]:
                    result[key] = []
                else:
                    result[key] = ""

        # 确保数组字段确实是数组
        for field in ["missing", "extra"]:
            if not isinstance(result[field], list):
                result[field] = [str(result[field])] if result[field] else []

        logger.debug(f"Hashtag audit completed: compliant={result['compliant']}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse hashtag audit result as JSON: {e}")
        # 降级：简单比较
        missing = [tag for tag in project_hashtags if tag not in content_hashtags]
        extra = [tag for tag in content_hashtags if tag not in project_hashtags]
        compliant = len(missing) == 0 if strict_mode else len(missing) <= len(project_hashtags) // 2

        return {
            "compliant": compliant,
            "missing": missing,
            "extra": extra,
            "analysis": f"LLM解析失败，使用简单字符串匹配。缺失：{missing}，额外：{extra}"
        }
    except Exception as e:
        logger.error(f"LLM hashtag audit failed: {e}")
        return {
            "compliant": True,
            "missing": [],
            "extra": [],
            "analysis": f"审核失败：{str(e)}"
        }


def audit_benefits_vs_project(client, model: str, content_benefits: list,
                             project_standards: str, fuzzy_mode: bool = False) -> Dict[str, Any]:
    """使用LLM进行利益点合规性审核

    Args:
        client: Anthropic client实例
        model: 使用的模型名称
        content_benefits: 内容中提取的利益点列表
        project_standards: 项目的利益点标准要求
        fuzzy_mode: 模糊匹配模式，True时允许语义相近的表述

    Returns:
        dict: {"compliant": bool, "violations": [str], "analysis": str}
    """
    if not content_benefits:
        return {
            "compliant": not bool(project_standards.strip()),
            "violations": ["内容中未找到利益点描述"] if project_standards.strip() else [],
            "analysis": "内容中未找到利益点" + ("，但项目有要求" if project_standards.strip() else "")
        }

    if not project_standards.strip():
        return {
            "compliant": True,
            "violations": [],
            "analysis": f"项目无特殊利益点要求，内容包含{len(content_benefits)}个利益点"
        }

    audit_prompt = f"""你是一个专业的内容审核员，负责检查利益点描述的合规性。

项目利益点标准要求：
{project_standards}

内容实际的利益点：
{chr(10).join([f"- {benefit}" for benefit in content_benefits])}

请分析利益点合规性，考虑以下因素：
1. 利益点是否符合项目标准
2. 是否有夸大宣传或虚假承诺
3. 审核模式：{"模糊匹配（允许语义相近的表述）" if fuzzy_mode else "精确匹配（严格按标准要求）"}

请以JSON格式返回审核结果：
{{
    "compliant": true/false,
    "violations": ["违规问题描述"],
    "analysis": "详细分析说明"
}}"""

    try:
        logger.debug("Starting benefits compliance audit with LLM")

        msg = client.messages.create(
            model=model,
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": audit_prompt
            }]
        )

        result_text = msg.content[0].text.strip()
        logger.debug(f"LLM benefits audit raw result: {result_text[:200]}...")

        # 清理markdown格式
        if result_text.startswith('```json'):
            result_text = result_text.replace('```json', '').replace('```', '').strip()
        elif result_text.startswith('```'):
            result_text = result_text.replace('```', '').strip()

        result = json.loads(result_text)

        # 验证返回的数据结构
        required_keys = ["compliant", "violations", "analysis"]
        for key in required_keys:
            if key not in result:
                logger.warning(f"Missing key '{key}' in benefits audit result")
                if key == "compliant":
                    result[key] = True
                elif key == "violations":
                    result[key] = []
                else:
                    result[key] = ""

        # 确保violations字段确实是数组
        if not isinstance(result["violations"], list):
            result["violations"] = [str(result["violations"])] if result["violations"] else []

        logger.debug(f"Benefits audit completed: compliant={result['compliant']}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse benefits audit result as JSON: {e}")
        return {
            "compliant": True,
            "violations": [],
            "analysis": f"LLM解析失败，无法进行利益点合规性分析：{str(e)}"
        }
    except Exception as e:
        logger.error(f"LLM benefits audit failed: {e}")
        return {
            "compliant": True,
            "violations": [],
            "analysis": f"审核失败：{str(e)}"
        }


def audit_slogans_vs_project(client, model: str, content_slogans: list,
                           project_requirements: str, exact_mode: bool = True) -> Dict[str, Any]:
    """使用LLM进行口令/优惠码合规性审核

    Args:
        client: Anthropic client实例
        model: 使用的模型名称
        content_slogans: 内容中提取的口令/优惠码列表
        project_requirements: 项目的口令要求说明
        exact_mode: 精确匹配模式，True时要求严格按要求提供口令

    Returns:
        dict: {"compliant": bool, "violations": [str], "analysis": str}
    """
    if not project_requirements.strip():
        return {
            "compliant": True,
            "violations": [],
            "analysis": f"项目无口令要求，内容包含{len(content_slogans)}个疑似口令"
        }

    audit_prompt = f"""你是一个专业的内容审核员，负责检查口令/优惠码的合规性。

项目口令要求：
{project_requirements}

内容实际的口令/优惠码：
{content_slogans if content_slogans else "（未找到）"}

请分析口令合规性，考虑以下因素：
1. 是否按项目要求提供了口令
2. 口令格式是否正确
3. 审核模式：{"精确匹配（严格按要求）" if exact_mode else "宽松匹配（允许变体）"}

请以JSON格式返回审核结果：
{{
    "compliant": true/false,
    "violations": ["违规问题描述"],
    "analysis": "详细分析说明"
}}"""

    try:
        logger.debug("Starting slogans compliance audit with LLM")

        msg = client.messages.create(
            model=model,
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": audit_prompt
            }]
        )

        result_text = msg.content[0].text.strip()
        logger.debug(f"LLM slogans audit raw result: {result_text[:200]}...")

        # 清理markdown格式
        if result_text.startswith('```json'):
            result_text = result_text.replace('```json', '').replace('```', '').strip()
        elif result_text.startswith('```'):
            result_text = result_text.replace('```', '').strip()

        result = json.loads(result_text)

        # 验证返回的数据结构
        required_keys = ["compliant", "violations", "analysis"]
        for key in required_keys:
            if key not in result:
                logger.warning(f"Missing key '{key}' in slogans audit result")
                if key == "compliant":
                    result[key] = True
                elif key == "violations":
                    result[key] = []
                else:
                    result[key] = ""

        # 确保violations字段确实是数组
        if not isinstance(result["violations"], list):
            result["violations"] = [str(result["violations"])] if result["violations"] else []

        logger.debug(f"Slogans audit completed: compliant={result['compliant']}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse slogans audit result as JSON: {e}")
        return {
            "compliant": True,
            "violations": [],
            "analysis": f"LLM解析失败，无法进行口令合规性分析：{str(e)}"
        }
    except Exception as e:
        logger.error(f"LLM slogans audit failed: {e}")
        return {
            "compliant": True,
            "violations": [],
            "analysis": f"审核失败：{str(e)}"
        }


def project_specific_audit_engine(client, model: str, content: str, extracted_elements: Dict[str, Any],
                                project_config: Dict[str, str], audit_modes: Dict[str, bool],
                                row_slogan_requirements: str = "") -> Dict[str, Any]:
    """执行项目特定的审核引擎

    Args:
        client: Anthropic client实例
        model: 使用的模型名称
        content: 原始内容文本
        extracted_elements: 提取的内容元素（hashtags, benefits, slogans等）
        project_config: 项目配置信息
        audit_modes: 审核模式配置
        row_slogan_requirements: 该行数据的口令词要求（来自飞书表格）

    Returns:
        dict: 包含所有审核结果的综合报告
    """
    logger.info(f"Starting project-specific audit for: {project_config.get('项目名称', 'Unknown')}")

    audit_results = {
        "project_name": project_config.get('项目名称', ''),
        "overall_passed": True,
        "overall_violations": [],
        "audit_details": {},
        "summary": ""
    }

    try:
        # 1. 话题标签审核
        if audit_modes.get("hashtag_strict", True):
            project_hashtags = [tag.strip() for tag in project_config.get('话题标签', '').split(',') if tag.strip()]
            content_hashtags = extracted_elements.get('hashtags', [])

            hashtag_result = audit_hashtags_vs_project(
                client, model, content_hashtags, project_hashtags, strict_mode=True
            )
            audit_results["audit_details"]["hashtag_audit"] = hashtag_result

            if not hashtag_result["compliant"]:
                audit_results["overall_passed"] = False
                audit_results["overall_violations"].append("话题标签不符合项目要求")

        # 2. 利益点审核
        if audit_modes.get("benefit_fuzzy", False):
            project_benefit_standards = project_config.get('利益点标准', '')
            content_benefits = extracted_elements.get('benefits', [])

            benefit_result = audit_benefits_vs_project(
                client, model, content_benefits, project_benefit_standards, fuzzy_mode=True
            )
            audit_results["audit_details"]["benefit_audit"] = benefit_result

            if not benefit_result["compliant"]:
                audit_results["overall_passed"] = False
                audit_results["overall_violations"].extend(benefit_result["violations"])

        # 3. 口令审核
        if audit_modes.get("slogan_exact", True):
            project_slogan_requirements = row_slogan_requirements  # 使用飞书表格中的口令词
            content_slogans = extracted_elements.get('slogans', [])

            slogan_result = audit_slogans_vs_project(
                client, model, content_slogans, project_slogan_requirements, exact_mode=True
            )
            audit_results["audit_details"]["slogan_audit"] = slogan_result

            if not slogan_result["compliant"]:
                audit_results["overall_passed"] = False
                audit_results["overall_violations"].extend(slogan_result["violations"])

        # 生成综合总结
        passed_audits = sum(1 for audit in audit_results["audit_details"].values() if audit.get("compliant", True))
        total_audits = len(audit_results["audit_details"])

        audit_results["summary"] = f"项目专项审核完成：{passed_audits}/{total_audits} 项通过"
        if audit_results["overall_violations"]:
            audit_results["summary"] += f"，发现 {len(audit_results['overall_violations'])} 个问题"

        logger.info(f"Project audit completed for {project_config.get('项目名称')}: "
                   f"passed={audit_results['overall_passed']}")

    except Exception as e:
        logger.error(f"Project specific audit failed: {e}")
        audit_results["overall_passed"] = True  # 失败时不阻断流程
        audit_results["summary"] = f"项目专项审核失败：{str(e)}"

    return audit_results