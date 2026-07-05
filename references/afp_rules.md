# AFP 计费规则参考

> 验证日期：2026-07-05
> 官方文档：https://www.volcengine.com/docs/82379/2516283
> ARK定价页：https://www.volcengine.com/docs/82379/1544106
> 套餐概览：https://www.volcengine.com/docs/82379/2366394

## 一、两种计费模式

| | ARK 按量计费 | Agent Plan (AFP) |
|---|---|---|
| provider | `ark` | `volcengine-agent-plan` |
| billing_type | `pay_as_you_go` | `afp` |
| 计费单位 | 元/百万token | AFP点数 |
| 计算公式 | input×单价 + output×单价 | (input×系数 + output×系数)/10000 |
| 适用场景 | 按量后付费，用多少付多少 | 包月套餐，AFP点数从月额度扣除 |

## 二、ARK 按量计费定价（元/百万token）

### 分段计费模型

| 模型 | 0-32k input/output | 32k-128k input/output | >128k input/output |
|------|-------|-------|-------|
| doubao-seed-2.0-mini | 0.2 / 2.0 | 0.4 / 4.0 | 0.8 / 8.0 |
| doubao-seed-2.0-pro | 3.2 / 16.0 | 4.8 / 24.0 | 9.6 / 48.0 |
| doubao-seed-2.0-code | 3.2 / 16.0 | 4.8 / 24.0 | 9.6 / 48.0 |
| glm-4.7 | 3.0 / 14.0 | 4.0 / 16.0 | - |

> 注：glm-4.7 官方定价还区分输出长度（≤0.2k vs >0.2k），此处取 >0.2k 档位（Agent场景更常见）

### 扁平计费模型（不分段）

| 模型 | input | output | cache_read |
|------|-------|--------|------------|
| deepseek-v4-flash | 1.00 | 2.00 | 0.20 |
| deepseek-v4-pro | 12.00 | 24.00 | 1.00 |
| kimi-k2.6 | 6.50 | 27.00 | 1.10 |

> 注：kimi-k2.6 未列入ARK官方定价页，价格为Kimi开放平台直连定价，通过ARK调用时以实际账单为准

## 三、Agent Plan (AFP) 系数计费

### AFP计算公式
```
AFP = (input_tokens × input_coef + output_tokens × output_coef) / 10000
```
- input_coef = 模型基础系数 × 上下文分段倍率（0.67/1/2）
- output_coef = 模型基础系数（不分段）

### 模型系数表

| 模型 | 0-32k input/output | 32k-128k input/output | >128k input/output |
|------|-------|-------|-------|
| doubao-seed-2.0-mini | 0.167 / 0.25 | 0.25 / 0.25 | 0.5 / 0.25 |
| deepseek-v4-flash | 0.335 / 0.5 | 0.5 / 0.5 | 1.0 / 0.5 |
| doubao-seed-2.0-code | 1.675 / 2.5 | 2.5 / 2.5 | 5.0 / 2.5 |
| deepseek-v4-pro | 3.685 / 5.5 | 5.5 / 5.5 | 11.0 / 5.5 |
| kimi-k2.6 | 3.015 / 4.5 | 4.5 / 4.5 | 9.0 / 4.5 |

### 限时折扣

2026-06-10 ~ 2026-07-15：deepseek-v4-pro、kimi-k2.6、kimi-k2.7-code、glm-5.2 享最低2.5折优惠。
工具使用标准系数（非折扣系数），折扣期内计算值会略高于控制台实际消耗。

## 四、Agent Plan 套餐限额

| 套餐 | 价格 | 月度AFP | 周度AFP | 5小时AFP |
|------|------|---------|---------|----------|
| Small | 40元/月 | 20,000 | 7,000 | 2,000 |
| Medium | 200元/月 | 100,000 | 35,000 | 10,000 |
| Large | 500元/月 | 250,000 | 87,500 | 25,000 |
| Max | 1000元/月 | 500,000 | 175,000 | 50,000 |

> 周度和5小时额度为估算值（月度/4.3 和 /10），以官方控制台为准

## 五、变更记录

- v1.4.0 (2026-07-05): ARK模型从AFP改为按量计费(pay_as_you_go)，calculator.py同步支持分段按量计费
- v1.3.1 (2026-07-05): VP模型系数从0修正为官方标准值
- v1.3.0 (2026-07-05): 初始AFP系数配置
