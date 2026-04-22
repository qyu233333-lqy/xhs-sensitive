"""项目配置管理核心模块"""

import csv
import json
import os
import logging
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

# 项目配置缓存
_project_configs_cache = {}


def get_project_config_csv_path(csv_path: Optional[str] = None) -> str:
    """获取项目配置 CSV 的绝对路径。"""
    from .config import load_config  # 避免循环导入

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if csv_path is None:
        config = load_config()
        csv_path = config.get("project_config_path", os.path.join(base_dir, "ref.csv"))

    if not os.path.isabs(csv_path):
        csv_path = os.path.join(base_dir, csv_path)
    return csv_path

def load_project_configs(csv_path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """从CSV文件加载项目配置，返回 {项目名称: 配置字典} 格式

    Args:
        csv_path: CSV文件路径，默认使用config中的project_config_path或BASE_DIR/ref.csv

    Returns:
        dict: {项目名称: {项目介绍:, 话题标签:, 利益点标准:, ...}}

    Raises:
        FileNotFoundError: CSV文件不存在
        ValueError: CSV格式错误或必需字段缺失
    """
    global _project_configs_cache

    if csv_path is None:
        from .config import load_config  # 避免循环导入
        config = load_config()

        # Check if project audit is enabled
        if not config.get("enable_project_audit", True):
            logger.info("Project audit is disabled in configuration")
            return {}

        feishu_url = config.get("project_config_feishu_url", "").strip()
        if feishu_url:
            return load_project_configs_from_feishu(feishu_url)

    csv_path = get_project_config_csv_path(csv_path)

    # 检查缓存
    cache_key = f"{csv_path}_{os.path.getmtime(csv_path) if os.path.exists(csv_path) else 0}"
    if cache_key in _project_configs_cache:
        logger.debug(f"Using cached project configs for {csv_path}")
        return _project_configs_cache[cache_key]

    if not os.path.exists(csv_path):
        logger.error(f"Project config file not found: {csv_path}")
        raise FileNotFoundError(f"项目配置文件不存在: {csv_path}")

    try:
        project_configs = {}
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            # 验证必需的列是否存在
            required_fields = ['项目名称', '话题标签', '利益点标准']
            missing_fields = [field for field in required_fields if field not in reader.fieldnames]
            if missing_fields:
                raise ValueError(f"CSV文件缺失必需字段: {missing_fields}")

            for row_idx, row in enumerate(reader, start=2):  # 从第2行开始（考虑表头）
                project_name = row.get('项目名称', '').strip()
                if not project_name:
                    logger.warning(f"第{row_idx}行项目名称为空，跳过")
                    continue

                # 验证当前行配置
                try:
                    config_dict = validate_project_config(row)
                    project_configs[project_name] = config_dict
                    logger.debug(f"Loaded project config: {project_name}")
                except ValueError as e:
                    logger.warning(f"第{row_idx}行配置验证失败: {e}")
                    continue

        if not project_configs:
            logger.warning("No valid project configurations found in CSV")

        # 缓存结果
        _project_configs_cache[cache_key] = project_configs
        logger.info(f"Loaded {len(project_configs)} project configurations from {csv_path}")
        return project_configs

    except csv.Error as e:
        logger.error(f"CSV parsing error: {e}")
        raise ValueError(f"CSV文件格式错误: {e}")
    except Exception as e:
        logger.error(f"Failed to load project configs: {e}")
        raise


def get_project_config(project_name: str, configs: Optional[Dict[str, Dict]] = None) -> Optional[Dict[str, Any]]:
    """根据项目名称获取配置，支持精确匹配和模糊匹配

    Args:
        project_name: 要查找的项目名称
        configs: 项目配置字典，如果为None则重新加载

    Returns:
        dict or None: 匹配的项目配置，如果找不到返回None
    """
    if not project_name or not project_name.strip():
        return None

    if configs is None:
        try:
            configs = load_project_configs()
        except (FileNotFoundError, ValueError) as e:
            logger.error(f"Failed to load project configs: {e}")
            return None

    if not configs:
        return None

    project_name = project_name.strip()

    # 1. 精确匹配（优先级最高）
    if project_name in configs:
        logger.debug(f"Exact match found for project: {project_name}")
        return configs[project_name]

    # 2. 包含匹配（项目名称包含在配置的key中）
    for config_name, config in configs.items():
        if project_name in config_name:
            logger.debug(f"Partial match found: {project_name} -> {config_name}")
            return config

    # 3. 反向包含匹配（配置的key包含在项目名称中）
    for config_name, config in configs.items():
        if config_name in project_name:
            logger.debug(f"Reverse partial match found: {project_name} -> {config_name}")
            return config

    logger.debug(f"No project config found for: {project_name}")
    return None


def validate_project_config(config_row: Dict[str, str]) -> Dict[str, str]:
    """验证项目配置的完整性和有效性

    Args:
        config_row: CSV行数据字典

    Returns:
        dict: 验证后的配置字典

    Raises:
        ValueError: 配置无效时抛出异常
    """
    if not isinstance(config_row, dict):
        raise ValueError("配置必须是字典格式")

    # 检查必需字段
    required_fields = {
        '项目名称': '项目名称不能为空',
        '话题标签': '话题标签不能为空',
        '利益点标准': '利益点标准不能为空'
    }

    validated_config = {}
    for field, error_msg in required_fields.items():
        value = config_row.get(field, '').strip()
        if not value:
            raise ValueError(error_msg)
        validated_config[field] = value

    # 可选字段
    optional_fields = ['项目介绍', '口令要求', '审核严格度']  # 注意：口令要求现在从飞书表格"口令词"字段获取
    for field in optional_fields:
        value = config_row.get(field, '').strip()
        validated_config[field] = value

    # 验证话题标签格式（应该包含#符号）
    hashtags = validated_config['话题标签']
    if '#' not in hashtags:
        logger.warning(f"话题标签可能缺少#符号: {hashtags}")

    # 验证审核严格度
    audit_mode = validated_config.get('审核严格度', 'normal').lower()
    if audit_mode not in ['strict', 'normal', 'loose']:
        logger.warning(f"未知的审核严格度: {audit_mode}，使用默认值normal")
        validated_config['审核严格度'] = 'normal'

    return validated_config


def clear_project_configs_cache():
    """清除项目配置缓存"""
    global _project_configs_cache
    _project_configs_cache.clear()
    logger.info("Project configs cache cleared")


def load_project_configs_from_feishu(feishu_url: str) -> Dict[str, Dict[str, Any]]:
    """从飞书标准表加载项目配置。"""
    from .config import load_config
    from .feishu import fetch_feishu_sheet

    config = load_config()
    if not config.get("feishu_app_id") or not config.get("feishu_app_secret"):
        raise ValueError("缺少飞书应用配置，无法读取飞书审核标准表")

    cache_key = f"feishu::{feishu_url}"
    if cache_key in _project_configs_cache:
        logger.debug(f"Using cached project configs for {feishu_url}")
        return _project_configs_cache[cache_key]

    sheet_data = fetch_feishu_sheet(
        feishu_url,
        config["feishu_app_id"],
        config["feishu_app_secret"],
        auditable_only=False,
    )

    project_configs = {}
    for row_idx, row in enumerate(sheet_data.get("data", []), start=2):
        project_name = str(row.get("项目名称") or "").strip()
        if not project_name:
            logger.warning(f"飞书标准表第{row_idx}行项目名称为空，跳过")
            continue

        try:
            config_dict = validate_project_config({
                "项目名称": project_name,
                "项目介绍": str(row.get("项目介绍") or "").strip(),
                "话题标签": str(row.get("话题标签") or "").strip(),
                "利益点标准": str(row.get("利益点标准") or "").strip(),
                "口令要求": str(row.get("口令要求") or "").strip(),
                "审核严格度": str(row.get("审核严格度") or "normal").strip(),
            })
            project_configs[project_name] = config_dict
        except ValueError as e:
            logger.warning(f"飞书标准表第{row_idx}行配置验证失败: {e}")

    _project_configs_cache[cache_key] = project_configs
    logger.info(f"Loaded {len(project_configs)} project configurations from Feishu: {feishu_url}")
    return project_configs


def save_project_config(config_row: Dict[str, str], csv_path: Optional[str] = None) -> Dict[str, Any]:
    """新增或更新项目配置，并持久化到 CSV。"""
    csv_path = get_project_config_csv_path(csv_path)
    validated = validate_project_config(config_row)
    project_name = validated["项目名称"]

    fieldnames = ["项目名称", "项目介绍", "话题标签", "利益点标准", "口令要求", "审核严格度"]
    rows = []
    exists = False

    if os.path.exists(csv_path):
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                fieldnames = list(reader.fieldnames)
                for required in ["项目名称", "项目介绍", "话题标签", "利益点标准", "口令要求", "审核严格度"]:
                    if required not in fieldnames:
                        fieldnames.append(required)

            for row in reader:
                current_name = (row.get("项目名称") or "").strip()
                if current_name == project_name:
                    exists = True
                    merged = {key: row.get(key, "") for key in fieldnames}
                    merged.update(validated)
                    rows.append(merged)
                else:
                    rows.append({key: row.get(key, "") for key in fieldnames})

    if not exists:
        new_row = {key: "" for key in fieldnames}
        new_row.update(validated)
        rows.append(new_row)

    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    clear_project_configs_cache()
    logger.info("%s project config: %s", "Updated" if exists else "Created", project_name)
    return {"project_name": project_name, "action": "updated" if exists else "created", "csv_path": csv_path}


def get_project_config_for_review(project_name: str) -> Optional[Dict[str, Any]]:
    """为审核获取项目配置的便捷函数

    Args:
        project_name: 项目名称

    Returns:
        dict or None: 项目配置字典，未找到时返回None
    """
    try:
        configs = load_project_configs()
        return get_project_config(project_name, configs)
    except Exception as e:
        logger.error(f"Failed to get project config for {project_name}: {e}")
        return None


def get_project_config_exact_for_review(project_name: str) -> Optional[Dict[str, Any]]:
    """为审核获取项目配置，只允许按项目名称精确匹配。"""
    try:
        if not project_name or not project_name.strip():
            return None

        configs = load_project_configs()
        return configs.get(project_name.strip())
    except Exception as e:
        logger.error(f"Failed to get exact project config for {project_name}: {e}")
        return None
