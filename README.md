# 内容审核 Agent

基于 AI 的智能内容审核系统，支持飞书集成、项目化配置管理和批量审核功能。

## ✨ 功能特点

- 🤖 **AI 驱动审核**：使用 Claude API 进行智能内容分析和元素提取
- 📊 **飞书深度集成**：直接读取飞书表格数据，获取文档内容，写回结果并添加评论
- 🎯 **项目化配置**：支持 CSV 配置文件管理不同项目的审核标准
- 📝 **内容元素提取**：自动提取话题标签、利益点、口令等关键信息
- 🔍 **多层次审核**：项目特定审核 + 通用内容审核的双重保障
- 📁 **多格式支持**：支持飞书表格、Excel (.xlsx) 文件处理
- 🏗️ **模块化架构**：基于 Flask 蓝图的现代化 Web 应用架构
- 🔒 **安全可靠**：完善的错误处理、日志记录和输入验证

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境

复制配置模板并编辑：

```bash
cp config.example.json config.json
# 编辑 config.json，填入你的 API 密钥和配置
```

或使用环境变量：

```bash
export ANTHROPIC_API_KEY="sk-your-api-key"
export FEISHU_APP_ID="cli_your-app-id"
export FEISHU_APP_SECRET="your-app-secret"
export PROJECT_CONFIG_PATH="ref.csv"
```

### 3. 设置项目配置

复制项目配置模板：

```bash
cp ref.example.csv ref.csv
# 编辑 ref.csv，配置你的项目审核标准
```

### 4. 启动应用

```bash
python app.py
```

访问 http://localhost:8000 开始使用。

## ⚙️ 配置说明

### 主配置文件 (config.json)

```json
{
  "api_key": "sk-your-claude-api-key",
  "base_url": "https://api.anthropic.com",
  "model": "claude-sonnet-4-6",
  "feishu_app_id": "cli_your-feishu-app-id",
  "feishu_app_secret": "your-feishu-app-secret",
  "project_config_path": "ref.csv",
  "enable_project_audit": true,
  "audit_modes": {
    "hashtag_strict": true,
    "benefit_fuzzy": false,
    "slogan_exact": true
  },
  "project_audit_settings": {
    "auto_match_projects": true,
    "fallback_to_general_audit": true,
    "max_project_details": 5,
    "include_project_info_in_notes": true
  }
}
```

### 项目配置文件 (ref.csv)

支持为不同项目配置专属的审核标准：

| 项目名称 | 项目介绍 | 话题标签 | 利益点标准 | 审核严格度 |
|---------|---------|---------|-----------|-----------|
| 南京大牌档 | 连锁餐厅品牌 | #美团黑钻会员 #南京大牌档 #联合会员 | 黑钻直升「状元」;黑金晋「贡士」;绑定即领满100-50进店见面礼 | strict |
| 跑腿1对1急送 | 急送服务权益 | #美团黑钻会员 #跑腿急送 #1对1急送 | 黑钻会员每月可享1次免费跑腿1对1急送权益;比普通跑腿平均提速20分钟 | normal |
| 酒店权益 | 酒店住宿优惠 | #美团会员住酒店 #美团会员酒店权益 #美团黑钻会员 | 白银及以上订酒店最高85折;免费升房/免费早餐;黑钻会员每月还可领2000积分抵房费 | normal |

**重要说明**：
- ✅ **话题标签**：从 `ref.csv` 获取项目要求的标签
- ✅ **利益点标准**：从 `ref.csv` 获取项目的利益点要求
- ⚠️ **口令要求**：已改为从飞书表格的"口令词"字段获取，**不再使用** `ref.csv` 中的配置

#### 配置字段说明

- **项目名称**：用于匹配飞书表格中的"权益类型"字段
- **项目介绍**：项目描述（可选）
- **话题标签**：必须包含的话题标签，用空格分隔
- **利益点标准**：必须包含的利益点描述，用分号分隔
- **审核严格度**：
  - `strict`：严格模式，必须完全匹配
  - `normal`：普通模式，允许部分匹配
  - `loose`：宽松模式，语义相近即可

### 环境变量支持

- `ANTHROPIC_API_KEY`: Claude API 密钥
- `API_BASE_URL`: API 基础 URL
- `CLAUDE_MODEL`: 使用的模型名称
- `FEISHU_APP_ID`: 飞书应用 ID
- `FEISHU_APP_SECRET`: 飞书应用密钥
- `PROJECT_CONFIG_PATH`: 项目配置 CSV 路径
- `ENABLE_PROJECT_AUDIT`: 是否启用项目专项审核
- `LOG_LEVEL`: 日志级别 (DEBUG/INFO/WARNING/ERROR)

## 📖 使用方法

### 🎯 审核逻辑说明

系统根据以下逻辑进行智能审核：

1. **项目配置匹配**：根据表格中的"权益类型"字段匹配 `ref.csv` 中的项目配置
2. **三重审核机制**：
   - **话题标签审核**：检查是否包含项目要求的话题标签（从 `ref.csv` 获取）
   - **利益点审核**：验证利益点是否符合项目标准（从 `ref.csv` 获取）
   - **口令审核**：检查稿件中的口令是否与表格"口令词"字段完全一致
3. **综合判断**：任一项不符合要求则审核失败，在"AI审核状态（内部）"字段标注结果和原因

### 方式一：飞书表格审核

#### 第一步：飞书应用权限配置

**重要**：如果使用飞书多维表格（Bitable），需要为飞书应用申请特殊权限：

1. 点击权限申请链接：[申请 Bitable 权限](https://open.feishu.cn/app/cli_a95dc262c639dbcf/auth?q=bitable:app:readonly,bitable:app,base:record:retrieve&op_from=openapi&token_type=tenant)
2. 选择以下权限之一：
   - `bitable:app:readonly`（推荐）
   - `bitable:app`
   - `base:record:retrieve`
3. 完成权限申请后即可正常使用

#### 第二步：使用流程

1. 打开 http://localhost:8000
2. 在设置页面配置：
   - Claude API 密钥
   - 飞书应用 ID 和 Secret
3. 返回主页，粘贴飞书表格分享链接
4. 点击"解析"，系统自动解析表格结构
5. 确认数据无误后，点击"开始审核"
6. 系统会实时显示审核进度，完成后：
   - 自动写回审核结果到原飞书表格
   - 为违规内容添加飞书文档评论
   - 生成详细的审核报告文件

### 方式二：Excel 文件审核（推荐用于测试）

#### 操作流程

1. 准备 Excel 文件，确保包含必需列（见下方格式要求）
2. 打开 http://localhost:8000
3. 点击"上传文件"，选择 Excel (.xlsx) 文件
4. 系统解析文件后显示数据预览
5. 点击"开始审核"
6. 审核完成后下载包含结果的 Excel 文件

#### 测试示例

创建测试文件 `test_data.xlsx`：

| 昵称 | 稿件链接 | 权益类型 | 口令词 | AI审核状态（内部） | 违规原因 | 备注 |
|------|----------|----------|--------|--------------------|----------|------|
| 测试用户1 | 测试文案：#美团黑钻会员 #南京大牌档 黑钻直升「状元」，NJDPD2024 | 南京大牌档 | NJDPD2024 | | | 测试 |
| 测试用户2 | 测试文案：#美团黑钻会员 #跑腿急送 黑钻会员专享权益，TEST123 | 跑腿1对1急送 | WRONG123 | | | 测试 |

运行审核后：
- 测试用户1：✅ 通过（话题标签、利益点、口令都匹配）
- 测试用户2：❌ 失败（口令词不匹配：要求WRONG123，实际TEST123）

### 📋 表格格式要求

#### 必需列

- **昵称**：创作者账号名称
- **稿件链接**：内容文本或飞书文档链接
- **权益类型**：项目/品牌名称（用于匹配 `ref.csv` 项目配置）
- **口令词**：该条数据要求的具体口令（用于口令审核）

#### 可选列

- **AI审核状态（内部）**：系统将在此列写入审核结果
- **违规原因**：系统将写入具体违规问题
- **备注**：自定义备注信息

#### 系统自动添加的列

- **AI审核**：审核结果图标（✅ 通过 / ❌ 失败 / ⏭️ 跳过）
- **审核备注**：详细审核信息和项目匹配结果
- **稿件内容**：提取的内容预览（前500字符）
- **处理时间**：审核完成的具体时间

## 🧪 开发相关

### 项目架构

```
├── app.py                    # 主应用入口（Flask 应用工厂）
├── core/                     # 核心业务逻辑模块
│   ├── __init__.py
│   ├── config.py            # 配置管理
│   ├── project.py           # 项目配置管理
│   ├── content_extraction.py # LLM 内容元素提取
│   ├── audit_engine.py      # 审核引擎
│   ├── feishu.py           # 飞书 API 集成
│   ├── file_utils.py       # 文件处理工具
│   └── review_engine.py    # 主审核流程
├── routes/                   # Flask 蓝图路由
│   ├── __init__.py
│   ├── main.py             # 主页面路由
│   └── api.py              # API 端点路由
├── templates/               # HTML 模板
├── uploads/                # 文件上传目录
├── results/                # 审核结果目录
├── test_app.py             # 完整测试套件
├── config.example.json     # 配置文件模板
├── ref.example.csv         # 项目配置模板
├── PROJECT_CONFIG.md       # 项目配置详细文档
├── CLAUDE.md              # Claude Code 开发指南
└── README.md              # 项目文档
```

### 运行测试

```bash
# 运行所有测试
python -m pytest test_app.py -v

# 运行特定测试模块
python -m pytest test_app.py::TestProjectConfigs -v
python -m pytest test_app.py::TestLLMContentExtraction -v
python -m pytest test_app.py::TestProjectAuditEngine -v

# 生成测试覆盖率报告
python -m pytest test_app.py --cov=core --cov=routes --cov-report=html
```

### 开发命令

```bash
# 启动开发服务器
python app.py

# 检查配置
python -c "from core.config import load_config; print(load_config())"

# 验证项目配置
python -c "from core.project import load_project_configs; print(len(load_project_configs()))"

# 测试 LLM 连接
python -c "from core.audit_engine import review_one; from core.config import load_config; import anthropic; cfg=load_config(); client=anthropic.Anthropic(api_key=cfg['api_key']); print(review_one(client, cfg.get('model', 'claude-sonnet-4-6'), '测试内容'))"
```

## 🎯 实际应用场景

### 品牌方内容审核

**使用场景**：某品牌推广活动，需要审核大量KOL/达人的营销内容

**审核要求**：
- 必须包含指定话题标签（如 #品牌名 #活动主题）
- 必须提及核心利益点（如 "满100减50" "免费体验"）  
- 必须使用正确的活动口令（如 "BRAND2024"）

**传统方式**：人工逐一检查，耗时费力，容易遗漏

**本系统方案**：
1. 在 `ref.csv` 配置项目审核标准
2. 在飞书表格"口令词"列填入每个KOL的专属口令
3. 系统自动批量审核，标注违规内容
4. 一键导出结果，违规内容自动标红

### 电商平台内容合规

**使用场景**：电商平台商家推广内容合规检查

**审核要求**：
- 检查是否包含平台要求的标签
- 验证优惠信息是否属实
- 确保使用了正确的推广码

**系统优势**：
- 支持千级别数据批量处理
- AI理解语义，避免机械匹配误判
- 实时进度显示，支持大文件处理

### 社交媒体营销监控

**使用场景**：监控合作达人是否按要求发布内容

**工作流程**：
1. 运营团队在飞书表格中维护达人合作信息
2. 定期收集达人发布的内容
3. 批量审核是否符合合作要求
4. 自动生成合规报告

## 🔧 核心功能详解

### LLM 内容元素提取

系统使用 Claude API 自动提取内容中的关键元素：

- **话题标签**: #开头的标签和变体形式
- **口令/优惠码**: 活动代码、兑换码、促销码
- **利益点**: 优惠、权益、福利描述
- **品牌提及**: 品牌名称、商家名称
- **标题和核心文案**: 结构化内容组织

### 项目化审核引擎

支持三种专项审核模式：

1. **话题标签审核**（hashtag_strict）
   - 检查是否包含项目要求的标签
   - 支持语义相似性匹配（如 #美食 和 #美食探店 认为相关）
   - 严格/宽松模式可配置

2. **利益点审核**（benefit_fuzzy）
   - 验证利益点是否符合项目标准
   - 检测夸大宣传和虚假承诺
   - 精确/模糊匹配可配置

3. **口令审核**（slogan_exact）
   - 验证是否按要求提供口令
   - **重要**：口令必须与飞书表格"口令词"字段**完全一致**
   - 区分大小写，一个字符不同都会审核失败

## ⚠️ 安全注意事项

- `config.json` 包含敏感 API 密钥，请勿提交到版本控制
- 上传文件大小限制 50MB，自动验证文件类型
- 文件名自动清理，防止路径遍历攻击
- 所有外部 API 调用包含超时和错误处理
- 飞书 API 访问令牌自动管理和刷新

## 🐛 故障排除

### 常见问题

1. **Claude API 连接失败**
   ```bash
   # 检查 API 密钥和网络
   curl -H "Authorization: Bearer $ANTHROPIC_API_KEY" https://api.anthropic.com/v1/messages
   ```

2. **飞书集成问题**

   **问题：获取表格数据失败: not found sheetId**
   ```
   解决方案：该表格是Bitable（多维表格），需要特殊权限
   1. 点击链接申请权限：https://open.feishu.cn/app/cli_a95dc262c639dbcf/auth?q=bitable:app:readonly
   2. 选择 bitable:app:readonly 权限
   3. 重新测试飞书表格功能
   ```

   **问题：HTTP 404 错误**
   ```
   解决方案：API版本问题，已修复为使用 v2 端点
   - 确认应用权限：读取云文档、读写电子表格  
   - 检查分享链接权限设置
   - 验证应用 ID 和密钥正确性
   ```

3. **项目配置问题**
   ```bash
   # 验证 CSV 格式和项目配置
   python -c "from core.project import load_project_configs; configs=load_project_configs(); print(f'加载了 {len(configs)} 个项目配置'); print(list(configs.keys()))"
   
   # 测试项目匹配
   python -c "from core.project import get_project_config_for_review; config=get_project_config_for_review('南京大牌档'); print('匹配结果:', config is not None)"
   ```

4. **文件处理错误**
   - 确认 Excel 文件格式为 .xlsx
   - 检查文件编码和特殊字符
   - 验证表头包含必需字段：昵称、稿件链接、权益类型、口令词

5. **口令审核问题**
   
   **问题：口令审核不生效**
   ```
   检查清单：
   ✅ 飞书表格是否有"口令词"列
   ✅ 口令词字段是否填写正确
   ✅ 稿件内容是否包含对应口令
   ✅ 口令是否完全匹配（区分大小写）
   ```

6. **端口占用问题**
   ```bash
   # 如果8000端口被占用，使用其他端口
   python -c "
   from app import create_app, setup_logging
   logger = setup_logging()
   app = create_app()
   app.run(host='0.0.0.0', port=9000, debug=False)
   "
   ```

### 日志分析

应用生成详细日志，查看方法：

```bash
# 实时监控日志
tail -f app.log

# 查看特定级别日志
grep ERROR app.log
grep WARNING app.log

# 搜索特定功能日志
grep "LLM" app.log
grep "Feishu" app.log
```

### 性能监控

```bash
# 检查文件大小
ls -lh uploads/ results/

# 监控内存使用
ps aux | grep python

# 检查磁盘空间
df -h
```

## 📚 相关文档

- [项目配置详细指南](PROJECT_CONFIG.md)
- [Claude Code 开发指南](CLAUDE.md)
- [API 端点文档](routes/api.py)
- [测试用例说明](test_app.py)

## 📄 许可证

MIT License - 详见 LICENSE 文件

## 🤝 贡献指南

1. Fork 项目仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开 Pull Request

## 🧪 快速测试指南

### 完整功能测试

1. **准备测试环境**
   ```bash
   # 克隆项目
   git clone <repository-url>
   cd content_review_web
   
   # 安装依赖
   pip install -r requirements.txt
   
   # 配置API密钥
   cp config.example.json config.json
   # 编辑 config.json，填入你的Claude API密钥和飞书配置
   ```

2. **创建测试数据**
   ```python
   # 运行以下代码创建测试文件
   from openpyxl import Workbook
   
   wb = Workbook()
   ws = wb.active
   
   # 添加表头
   headers = ['昵称', '稿件链接', '权益类型', '口令词', 'AI审核状态（内部）', '违规原因', '备注']
   ws.append(headers)
   
   # 添加测试数据
   test_data = [
       ['测试用户1', '测试文案：#美团黑钻会员 #南京大牌档 #联合会员 黑钻直升「状元」，绑定即领满100-50进店见面礼，NJDPD2024', '南京大牌档', 'NJDPD2024', '', '', '应该通过'],
       ['测试用户2', '测试文案：#美团黑钻会员 #跑腿急送 黑钻会员每月可享1次免费跑腿1对1急送权益，WRONG123', '跑腿1对1急送', 'CORRECT123', '', '', '应该失败-口令不匹配'],
       ['测试用户3', '测试文案：#其他标签 普通宣传内容，NJDPD2024', '南京大牌档', 'NJDPD2024', '', '', '应该失败-缺少必需标签']
   ]
   
   for row in test_data:
       ws.append(row)
   
   wb.save('test_audit.xlsx')
   print('测试文件 test_audit.xlsx 创建完成')
   ```

3. **启动应用并测试**
   ```bash
   # 启动应用
   python app.py
   # 如果8000端口被占用，可以修改app.py中的端口号
   ```

4. **执行测试流程**
   - 打开 http://localhost:8000
   - 上传 `test_audit.xlsx` 文件
   - 点击"开始审核"
   - 观察实时审核进度
   - 下载审核结果文件

5. **验证测试结果**
   预期结果：
   - **测试用户1**：✅ 通过（话题标签、利益点、口令都匹配）
   - **测试用户2**：❌ 失败（口令不匹配：需要CORRECT123，实际WRONG123）
   - **测试用户3**：❌ 失败（缺少必需的话题标签）

### 命令行快速测试

```bash
# 测试项目配置加载
python -c "from core.project import load_project_configs; print(f'加载项目配置: {len(load_project_configs())} 个')"

# 测试API连接
python -c "from core.config import load_config; from core.feishu import validate_feishu_config; cfg=load_config(); print('飞书配置:', validate_feishu_config(cfg.get('feishu_app_id', ''), cfg.get('feishu_app_secret', '')))"

# 测试内容提取
python -c "from core.content_extraction import extract_content_elements; from core.config import load_config; import anthropic; cfg=load_config(); client=anthropic.Anthropic(api_key=cfg['api_key']); result=extract_content_elements(client, cfg.get('model', 'claude-sonnet-4-6'), '测试内容 #测试标签 优惠50% TEST123'); print('提取结果:', result)"
```

### 性能基准测试

```bash
# 测试100条数据的处理时间
time python -c "
import time
from openpyxl import Workbook

# 创建大量测试数据
wb = Workbook()
ws = wb.active
headers = ['昵称', '稿件链接', '权益类型', '口令词']
ws.append(headers)

for i in range(100):
    ws.append([f'用户{i}', f'测试内容{i} #美团黑钻会员', '南京大牌档', 'TEST123'])

wb.save('benchmark_test.xlsx')
print('创建了100条测试数据')
"
```

## 📞 技术支持

如遇问题请：

1. 查看 [故障排除](#故障排除) 部分
2. 查看 [快速测试指南](#快速测试指南) 验证基础功能
3. 检查 `app.log` 日志文件
4. 提交 Issue 并附上：
   - 错误日志
   - 测试数据样例
   - 系统环境信息