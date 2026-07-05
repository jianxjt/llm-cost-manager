"""calculator.py — 计费引擎
支持按量计费（百炼/ARK）、AFP 系数计费（火山方舟Agent Plan），
以及套餐三层限额（月/周/5h）监控。
"""

import json
import sqlite3
from datetime import datetime, timedelta


def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_context_tier(input_tokens, tiers_config):
    """根据输入 token 数确定上下文分段区间"""
    for tier, _ in sorted(tiers_config.items(),
                          key=lambda x: _parse_tier_lower(x[0])):
        lower = _parse_tier_lower(tier)
        upper = _parse_tier_upper(tier)
        if upper is None or input_tokens <= upper:
            return tier
    return list(tiers_config.keys())[-1]


def _parse_tier_bound(val):
    """解析带 k 后缀的 token 数值，1k = 1024"""
    val = val.strip()
    if val.endswith('k'):
        return int(val[:-1]) * 1024
    return int(val)


def _parse_tier_lower(tier_str):
    """解析区间下限，如 '0-32k' → 0, '32k-128k' → 32768, '>128k' → 131072"""
    if tier_str.startswith('>'):
        return _parse_tier_bound(tier_str[1:])
    return _parse_tier_bound(tier_str.split('-')[0])


def _parse_tier_upper(tier_str):
    """解析区间上限，如 '0-32k' → 32768, '32k-128k' → 131072, '>128k' → None"""
    if tier_str.startswith('>'):
        return None
    parts = tier_str.split('-')
    if len(parts) == 2:
        return _parse_tier_bound(parts[1])
    return None


def calc_pay_as_you_go(input_tokens, output_tokens, cache_read_tokens,
                       pricing):
    """按量计费（百炼/ARK），返回费用（元）。
    支持分段计费（context_tiers）和扁平计费（input_per_mtok）。
    """
    tiers = pricing.get('context_tiers', {})
    if tiers:
        # 分段计费（如ARK豆包系列按上下文长度分档）
        tier = get_context_tier(input_tokens, tiers)
        coef = tiers[tier]
        input_cost = input_tokens * coef['input_per_mtok'] / 1_000_000
        output_cost = output_tokens * coef['output_per_mtok'] / 1_000_000
        cache_cost = cache_read_tokens * coef.get('cache_read_per_mtok', 0) / 1_000_000
    else:
        # 扁平计费（如百炼模型、ARK DeepSeek等不分档模型）
        input_cost = input_tokens * pricing['input_per_mtok'] / 1_000_000
        output_cost = output_tokens * pricing['output_per_mtok'] / 1_000_000
        cache_cost = cache_read_tokens * pricing.get('cache_read_per_mtok', 0) / 1_000_000
    return round(input_cost + output_cost + cache_cost, 4)


def calc_afp(input_tokens, output_tokens, pricing):
    """AFP 系数计费（火山方舟Agent Plan），返回消耗的 AFP 点数"""
    tiers = pricing.get('context_tiers', {})
    if not tiers:
        return 0.0
    tier = get_context_tier(input_tokens, tiers)
    coef = tiers[tier]
    afp = (input_tokens * coef['input_coef']
           + output_tokens * coef['output_coef']) / 10000
    return round(afp, 2)


def calculate_costs(db_path, models_config_path):
    """对未计费的记录进行费用计算并写回数据库"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    models_config = load_json(models_config_path)
    models = models_config.get('models', {})

    # 找出未计算费用的已知模型记录
    c.execute('''SELECT id, model_id, provider, input_tokens,
                 output_tokens, cache_read_tokens
                 FROM api_calls
                 WHERE cost_yuan IS NULL AND afp_consumed IS NULL
                   AND is_unknown = 0''')
    rows = c.fetchall()

    updated = 0
    for row in rows:
        rid, model_id, provider, inp, outp, cache_r = row
        # model_id 格式为 "provider/config_key"，提取 config_key 查配置
        config_key = model_id.split('/', 1)[1] if '/' in model_id else model_id
        model_info = models.get(config_key, {})
        pricing = model_info.get('pricing', {})
        billing = model_info.get('billing_type', '')

        if billing == 'pay_as_you_go':
            cost = calc_pay_as_you_go(inp, outp, cache_r, pricing)
            c.execute('UPDATE api_calls SET cost_yuan=? WHERE id=?',
                      (cost, rid))
        elif billing == 'afp':
            afp = calc_afp(inp, outp, pricing)
            c.execute('UPDATE api_calls SET afp_consumed=? WHERE id=?',
                      (afp, rid))
        elif billing == 'free':
            c.execute(
                'UPDATE api_calls SET cost_yuan=0, afp_consumed=0 '
                'WHERE id=?', (rid,))
        updated += 1

    conn.commit()
    conn.close()
    return {'calculated': updated}


def check_plan_limits(db_path, plans_config):
    """检查套餐三层限额消耗情况。
    plans_config 可以是 dict 或文件路径(str)。
    """
    if isinstance(plans_config, str):
        plans = load_json(plans_config)
    else:
        plans = plans_config
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    now = datetime.now()
    results = {}

    for plan_key, plan in plans.get('plans', {}).items():
        limits = plan.get('limits', {})
        result = {}

        # 月额度
        if 'monthly_afp' in limits:
            month_start = now.replace(day=1, hour=0, minute=0, second=0)
            c.execute('''SELECT COALESCE(SUM(afp_consumed),0)
                         FROM api_calls
                         WHERE afp_consumed IS NOT NULL
                           AND dt >= ?''',
                      (month_start.strftime('%Y-%m-%d %H:%M:%S'),))
            consumed = c.fetchone()[0]
            limit = limits['monthly_afp']
            pct = (consumed / limit * 100) if limit > 0 else 0
            result['monthly'] = {
                'consumed': round(consumed, 2),
                'limit': limit,
                'pct': round(pct, 1),
            }

        # 周额度
        if 'weekly_afp' in limits:
            week_start = now - timedelta(days=now.weekday())
            week_start = week_start.replace(hour=0, minute=0, second=0)
            c.execute('''SELECT COALESCE(SUM(afp_consumed),0)
                         FROM api_calls
                         WHERE afp_consumed IS NOT NULL
                           AND dt >= ?''',
                      (week_start.strftime('%Y-%m-%d %H:%M:%S'),))
            consumed = c.fetchone()[0]
            limit = limits['weekly_afp']
            pct = (consumed / limit * 100) if limit > 0 else 0
            result['weekly'] = {
                'consumed': round(consumed, 2),
                'limit': limit,
                'pct': round(pct, 1),
            }

        # 5小时额度
        if 'hourly_5_afp' in limits:
            five_hours_ago = (now - timedelta(hours=5)) \
                .strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''SELECT COALESCE(SUM(afp_consumed),0)
                         FROM api_calls
                         WHERE afp_consumed IS NOT NULL
                           AND dt >= ?''', (five_hours_ago,))
            consumed = c.fetchone()[0]
            limit = limits['hourly_5_afp']
            pct = (consumed / limit * 100) if limit > 0 else 0
            result['hourly_5'] = {
                'consumed': round(consumed, 2),
                'limit': limit,
                'pct': round(pct, 1),
            }

        results[plan_key] = result

    conn.close()
    return results
