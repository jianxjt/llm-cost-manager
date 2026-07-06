---
name: llm-cost-manager
version: 1.1.0
description: "一站式大模型调用成本管理工具，直连 OpenClaw 本地缓存，自主采集、自主计算、自主分析。支持按量后付费/AFP套餐/免费三种计费模式，覆盖阿里百炼、火山方舟、本地模型，提供增量采集、终端表格与Markdown报告输出。v1.1.0新增arkcli集成、分段系数自学习、百炼真实比例和Token效率分析。"
metadata:
  requires:
    bins: ["python3"]
  license: "MIT"
  author: "天地红尘"
---
# LLM Cost Manager

## 🔰 触发场景

- 用户询问 `LLM调用费用/成本/账单/统计`
- 用户要求查看 `token用量/模型使用情况/套餐消耗`
- 定时任务触发（如每日费用报告）
- 手动执行 `python bin/run.py` 命令

## 📦 前置条件

- Python 3.8+
- OpenClaw 缓存文件位于 `~/.openclaw/agents/*/sessions/.usage-cost-cache.json`
- 可选：`tabulate>=0.9.0`（用于优化终端表格输出，非强制）

## 🚀 安装部署

1. 克隆仓库到本地：`git clone https://github.com/jianxjt/llm-cost-manager.git`
2. 复制到 OpenClaw 技能目录：`cp -r llm-cost-manager ~/.openclaw/skills/`
3. （可选）安装依赖：`pip install tabulate>=0.9.0`
4. 首次运行自动生成配置模板

## 📖 使用方法

### 命令行模式

```bash
# 采集数据
python bin/run.py collect              # 增量采集所有 agent
python bin/run.py collect --agent main # 只采集 main agent
python bin/run.py collect --full       # 全量重采

# 生成报告
python bin/run.py report               # 默认：过去7天，终端输出
python bin/run.py report --agent main  # 只看 main agent
python bin/run.py report --since 2026-06-01  # 指定起始日期
python bin/run.py report --format markdown   # 输出 Markdown 文件
python bin/run.py report --with-arkcli       # 附加 arkcli AFP 实时数据+分段系数分析

# arkcli 数据采集
python bin/run.py arkcli collect       # 采集套餐AFP用量+分模型明细
python bin/run.py arkcli report        # 查看 arkcli 数据报告

# 配置管理
python bin/run.py config show          # 查看当前模型和套餐配置
python bin/run.py config set-plan      # 更新AFP套餐配置（支持命令行参数）
                                      # 示例: --monthly 100000 --weekly 35000 --hourly5 10000 --price 200 --name "Agent Plan Medium"
```

### 交互模式

直接在 OpenClaw 聊天窗口发送触发指令即可自动调用。

## ⚙️ 配置文件

所有配置文件位于 `config/` 目录：

- **openclaw.models.json** — 模型定义、定价、别名映射（核心配置，参考示例见文末）
- **pricing.json** — 各供应商官方定价快照（参考源）
- **plans.json** — 用户套餐配置（AFP 限额等）
- **tier_weights.json** — 分段系数自学习数据（自动生成，含 k 值和反算记录）

## 🔧 AFP 计费与套餐管理

### 计费规则

AFP（Agent Function Point）是火山引擎 Agent Plan 套餐的计费单位。计费规则详见 `references/afp_rules.md`。

**Agent 首次使用时应先读取该文件了解计费方式。** 如果文件缺失或内容过时（验证日期超过3个月），搜索"火山引擎 Agent Plan AFP 计费规则"获取最新规则并更新该文件。

### 套餐配置流程

**生成报告前，Agent 按以下步骤确认套餐配置：**

1. **读取本地配置**：检查 `config/plans.json`，确认 `start_date`/`end_date` 覆盖当前月份且 `monthly_afp` > 0
2. **配置缺失或过期时**：
   - 搜索"火山引擎 Agent Plan 套餐 额度 价格"获取当前可选套餐档位
   - 向用户展示搜索到的套餐选项，询问购买的是哪个档位
   - 用户确认后执行更新：
     ```bash
     python bin/run.py config set-plan --monthly <额度> --weekly <额度> --hourly5 <额度> --price <月费> --name "<套餐名>" --start-date YYYY-MM-01 --end-date YYYY-MM-30
     ```
   - 如用户不知道周度/5小时额度，只更新月度即可
3. **更新完成后**执行 `collect` 和 `report`

### 预警机制

**生成报告后，Agent 检查套餐消耗并判断是否预警：**

- 读取 `config/plans.json` 中的 `alert_thresholds`（默认 warning 80%, critical 90%）
- 对照报告中的 AFP 套餐状态区块，判断是否触发预警：
  - 消耗占比 ≥ warning_pct：提醒用户"AFP额度已使用 X%，注意控制用量"
  - 消耗占比 ≥ critical_pct：紧急提醒"AFP额度即将耗尽（剩余 X%），建议升级套餐或减少调用"
- 即使报告为 Markdown 格式，Agent 也应主动读取套餐状态并向用户汇报

### AFP 估算与分段系数自学习

arkcli 只返回 total_tokens（不分 input/output），工具通过以下机制实现高精度估算：

1. **真实比例获取**：从百炼/modelstudio 数据库提取真实 input/output 比例（实测 ~99.4%:0.6%），替代 80/20 假设
2. **分段系数 k 反算**：用 arkcli 套餐层面真实 AFP 反算有效分段系数 `k = (real_afp - B) / A`
   - k=1.0 表示全部在基础档（32k-128k）
   - k>1 表示部分调用上下文超过 128k（加价档）
   - k<1 表示部分调用在折扣档（≤32k）
3. **k 值自动保存**：反算结果存入 `config/tier_weights.json`，下次估算自动使用
4. **Token 效率分析**：报告输出 output/total 占比，识别大模型生成价值偏低问题

实测估算误差 < 0.1%（估算 11,306.4 vs 真实 11,306.6 AFP）。

**使用 `--with-arkcli` 参数**会在报告中附加：
- arkcli 分模型用量与 AFP 汇总
- Token 效率分析（input/output 比例 + 优化建议）
- 上下文分段系数分析（k 值 + 权重分布 + 优化建议）

## 💰 计费模式

| 模式 | 适用供应商 | 说明 |
|------|-----------|------|
| 按量后付费 | 阿里百炼 | 按 input/output/cache_read token 数 × 单价（元/百万token） |
| AFP 套餐 | 火山方舟 | 按 input/output token 数 × 上下文分段系数 / 10000 |
| 免费 | Ollama/本地模型 | 无费用产生 |

## 🗄️ 数据库

SQLite 数据库位于 `data/history.db`，包含以下表：

- `api_calls` — 每次 API 调用记录
- `collect_cursor` — 采集游标（增量采集）
- `arkcli_plan_usage` — arkcli 套餐级 AFP 用量快照
- `arkcli_model_daily` — arkcli 分模型每日 token 用量（含 AFP 消耗估算）
- `schema_version` — 数据格式版本

## 📤 输出格式

- **终端表格**：默认输出到 stdout，适合直接查看
- **Markdown**：`--format markdown` 输出到 `data/report.md`

## ⚠️ 注意事项

- V1.0.1 不直接修改 openclaw.json 原生配置
- V1.0.1 未知模型仅标记，不自动搜索定价
- 首次运行建议 `collect --full` 建立基线
- 后续运行默认增量采集，只处理新数据
- 所有数据处理在本地完成，无外部网络请求
- 时间戳使用系统本地时间
- arkcli 需先执行 `arkcli auth login` 登录授权
- VP（Agent Plan）模型 token=0 是已知问题，AFP 估算通过 arkcli 明细数据替代

## 📌 版本历史

- V1.0.1：provider-aware 架构，支持按量/AFP/免费三模式计费，终端/Markdown 报告，增量采集，跨平台路径兼容
- V1.1.0：arkcli 集成（套餐AFP采集+分模型明细），分段系数自学习（k值反算误差<0.1%），百炼真实input/output比例（~99.4%），Token效率分析，MODEL_MAPPING模型名映射，VP模型token=0修复
