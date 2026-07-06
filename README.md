# LLM Cost Manager

> 一站式大模型调用成本管理工具，直连 OpenClaw 本地缓存，自主采集、自主计算、自主分析。

## 为什么需要这个工具

OpenClaw 多 Agent 模式下，各 Agent 分别调用不同供应商的大模型（阿里百炼、火山方舟等），费用分散在各处，没有统一视角。这个工具直连 OpenClaw 本地缓存文件，自动采集所有 Agent 的调用记录，按模型、供应商、Agent、日期等维度汇总费用，让你一目了然地知道：

- 每天花了多少钱
- 哪个模型最贵
- 套餐额度还剩多少
- 哪个 Agent 调用最多

## 功能特性

- **自主采集**：直连 `~/.openclaw/agents/*/sessions/.usage-cost-cache.json`，无需 API
- **多供应商计费**：阿里百炼（按量后付费）、火山方舟（AFP 套餐）、Ollama（免费）
- **arkcli 集成**：自动采集火山引擎 Agent Plan 套餐 AFP 用量和分模型每日明细
- **分段系数自学习**：从真实 AFP 反算有效上下文分段系数，估算误差 < 0.1%
- **百炼真实比例**：从百炼数据获取真实 input/output 比例（~99.4%），替代 80/20 假设
- **Token 效率分析**：输出 token 占比分析，识别大模型生成价值偏低问题
- **增量采集**：基于游标机制，只处理新增数据
- **双格式报告**：终端表格 + Markdown 文件
- **本地运行**：所有数据处理在本地完成，无外部网络请求

## 快速开始

```bash
# 克隆仓库
git clone https://github.com/jianxjt/llm-cost-manager.git

# 进入目录
cd llm-cost-manager

# 首次全量采集
python bin/run.py collect --full

# 查看最近7天报告
python bin/run.py report

# 导出 Markdown 报告
python bin/run.py report --format markdown
```

## 使用方法

### 数据采集

```bash
python bin/run.py collect              # 增量采集所有 agent
python bin/run.py collect --agent main # 只采集指定 agent
python bin/run.py collect --full       # 全量重采（忽略游标）
```

### 费用报告

```bash
python bin/run.py report                          # 默认：过去7天，终端输出
python bin/run.py report --agent main             # 只看指定 agent
python bin/run.py report --since 2026-06-01       # 指定起始日期
python bin/run.py report --format markdown        # 输出 Markdown 文件
python bin/run.py report --with-arkcli            # 附加 arkcli AFP 实时数据+分段系数分析
```

### arkcli 数据采集

```bash
python bin/run.py arkcli collect                  # 采集套餐AFP用量+分模型明细
python bin/run.py arkcli report                   # 查看 arkcli 数据报告
python bin/run.py arkcli report --since 2026-06-15 # 指定起始日期
```

### 配置查看

```bash
python bin/run.py config show    # 查看当前模型和套餐配置
```

## 项目结构

```
llm-cost-manager/
├── bin/
│   └── run.py                 # CLI 主入口
├── core/
│   ├── __init__.py
│   ├── calculator.py          # 计费计算模块（含分段系数自学习）
│   ├── collector.py           # 数据采集模块
│   ├── arkcli_provider.py     # arkcli 数据采集与报告
│   └── reporter.py            # 报告生成模块
├── config/
│   ├── openclaw.models.json   # 模型定义与定价（核心配置）
│   ├── pricing.json           # 供应商官方定价快照
│   ├── plans.json             # 用户套餐配置
│   └── tier_weights.json      # 分段系数自学习数据（自动生成）
├── data/                      # 运行时数据（自动生成）
│   └── history.db             # SQLite 数据库
├── references/
│   └── afp_rules.md           # AFP 计费规则参考
├── tests/                     # 测试用例
├── requirements.txt           # Python 依赖
├── SKILL.md                   # OpenClaw Skill 描述文件
└── README.md                  # 本文件
```

## 计费模式说明

| 模式 | 适用供应商 | 计算方式 |
|------|-----------|---------|
| 按量后付费 | 阿里百炼 | input/output/cache_read token 数 × 单价（元/百万token） |
| AFP 套餐 | 火山方舟 | input/output token 数 × 上下文分段系数 / 10000 |

### AFP 估算与分段系数自学习

arkcli 只返回 total_tokens（不分 input/output），工具通过以下机制实现高精度估算：

1. **真实比例获取**：从百炼/modelstudio 数据库提取真实 input/output 比例（实测 ~99.4%:0.6%）
2. **分段系数 k 反算**：用 arkcli 套餐层面真实 AFP 反算有效分段系数 `k = (real_afp - B) / A`
3. **k 值自动保存**：反算结果存入 `config/tier_weights.json`，下次估算自动使用
4. **Token 效率分析**：报告输出 output/total 占比，识别生成价值偏低问题

实测估算误差 < 0.1%（估算 11,306.4 vs 真实 11,306.6 AFP）。
| 免费 | Ollama/本地模型 | 无费用产生 |

## 配置说明

核心配置文件 `config/openclaw.models.json` 示例：

```json
{
  "models": {
    "deepseek-v4-flash": {
      "display_name": "DeepSeek V4 Flash",
      "provider": "bailian",
      "billing_type": "pay_as_you_go",
      "pricing": {
        "input_per_mtok": 0.5,
        "output_per_mtok": 1.0,
        "cache_read_per_mtok": 0.1
      },
      "aliases": ["deepseek-chat", "deepseek-v4-flash-bailian"]
    }
  }
}
```

> **重要**：新增模型条目时必须填写 `aliases` 字段，包含该模型在 OpenClaw 缓存中的原始名称。否则采集时无法匹配。

## 技术要求

- Python 3.8+
- OpenClaw 已安装并运行（需要缓存文件）
- 可选：`tabulate>=0.9.0`（优化终端表格显示）

## License

MIT

## 作者

天地红尘（[GitHub](https://github.com/jianxjt)）
