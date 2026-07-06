"""calculator.py — 计费引擎
支持按量计费（百炼/ARK）、AFP 系数计费（火山方舟Agent Plan），
以及套餐三层限额（月/周/5h）监控。
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta


def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_tier_k(config_path=None):
    """加载上下文分段有效系数k（从config/tier_weights.json）
    k=1.0表示使用基础档（32k-128k）标准费率
    k由真实AFP数据反算得出，用于提高估算精度
    """
    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'config', 'tier_weights.json')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('current_k', 1.0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 1.0


def save_tier_k(k, real_afp, estimated_afp, config_path=None):
    """保存k值到config/tier_weights.json，记录历史"""
    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'config', 'tier_weights.json')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {'current_k': 1.0, 'history': []}

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    data['history'].append({
        'date': now_str,
        'k': k,
        'real_afp': round(real_afp, 2),
        'estimated_afp': round(estimated_afp, 2),
    })
    data['current_k'] = k
    data['last_updated'] = now_str

    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_real_io_ratio(db_path):
    """从百炼/modelstudio数据获取真实input/output比例
    返回input占比（0-1），无数据时返回0.8（默认80%）
    """
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''
            SELECT SUM(input_tokens), SUM(output_tokens)
            FROM api_calls
            WHERE provider IN ('bailian', 'modelstudio')
            AND input_tokens > 0 AND output_tokens > 0
        ''')
        row = c.fetchone()
        conn.close()
        if row and row[0] and row[1]:
            total_in = row[0]
            total_out = row[1]
            ratio = total_in / (total_in + total_out)
            return ratio
    except Exception:
        pass
    return 0.8  # fallback


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


def _find_model_by_provider_and_name(models, provider, model_name):
    """通过 provider 和模型名查找配置（支持 alias 匹配）"""
    for config_key, info in models.items():
        if info.get('provider') != provider:
            continue
        if config_key == model_name:
            return config_key, info
        if model_name in info.get('aliases', []):
            return config_key, info
    return None, None


def calculate_afp_from_arkcli(db_path, models_config_path, input_ratio=None):
    """从 arkcli 每日 token 数据计算 AFP 消耗

    arkcli 只返回 total_tokens（不区分 input/output），
    使用 input_ratio 估算 input/output 分配比例。

    Args:
        db_path: 数据库路径
        models_config_path: 模型配置文件路径
        input_ratio: input token 占比，None时自动从百炼数据获取真实比例
    Returns:
        dict: {'calculated': int, 'input_ratio': float}
    """
    # 先获取真实io比例（在打开数据库连接之前，避免SQLite锁定）
    if input_ratio is None:
        input_ratio = get_real_io_ratio(db_path)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    models_config = load_json(models_config_path)
    models = models_config.get('models', {})

    # 加载有效分段系数k（由真实AFP反算，初始=1.0即基础档32k-128k）
    config_dir = os.path.dirname(models_config_path)
    tier_weights_path = os.path.join(config_dir, 'tier_weights.json')
    k = load_tier_k(tier_weights_path)

    # 确保 afp_consumed 列存在
    c.execute("PRAGMA table_info(arkcli_model_daily)")
    columns = [col[1] for col in c.fetchall()]
    if columns and 'afp_consumed' not in columns:
        c.execute("ALTER TABLE arkcli_model_daily ADD COLUMN afp_consumed REAL")
        conn.commit()

    # 读取所有有 token 的记录（每次重新计算，确保系数变更后结果一致）
    try:
        c.execute('''
            SELECT id, date, model_id, tokens
            FROM arkcli_model_daily
            WHERE tokens > 0
        ''')
    except sqlite3.OperationalError:
        conn.close()
        return {'calculated': 0, 'input_ratio': input_ratio,
                'note': 'arkcli_model_daily 表不存在，请先执行 arkcli collect'}

    rows = c.fetchall()
    updated = 0

    for row in rows:
        rid, date_str, model_id, total_tokens = row
        if '/' in model_id:
            provider, model_name = model_id.split('/', 1)
        else:
            provider = ''
            model_name = model_id

        config_key, model_info = _find_model_by_provider_and_name(
            models, 'volcengine-agent-plan', model_name)

        if not model_info:
            continue

        pricing = model_info.get('pricing', {})
        billing = model_info.get('billing_type', '')

        if billing != 'afp':
            continue

        # 估算 input/output 分配
        input_tokens = int(total_tokens * input_ratio)
        output_tokens = total_tokens - input_tokens

        # 日聚合数据无法确定单次请求的上下文分段，
        # 使用有效系数k估算（k由真实AFP反算，初始=1.0即基础档32k-128k）
        # AFP规则：output_coef不分段（恒定），input_coef=基础系数×k
        tiers = pricing.get('context_tiers', {})
        if tiers:
            sorted_tiers = sorted(tiers.keys(),
                                  key=lambda x: _parse_tier_lower(x))
            # 取中间档作为基础档（3档时为32k-128k，即标准费率1x）
            base_tier = sorted_tiers[len(sorted_tiers) // 2]
            coef = tiers[base_tier]
            # input_coef = C_m × k（k是有效分段倍率）
            # output_coef = C_m（不分段，恒定）
            afp = (input_tokens * coef['input_coef'] * k
                   + output_tokens * coef['output_coef']) / 10000
            afp = round(afp, 2)
        else:
            afp = 0.0

        c.execute('UPDATE arkcli_model_daily SET afp_consumed=? WHERE id=?',
                  (afp, rid))
        updated += 1

    conn.commit()
    conn.close()
    return {'calculated': updated, 'input_ratio': input_ratio, 'k': k}


def back_calculate_k(db_path, models_config_path, real_total_afp):
    """从arkcli真实AFP反算上下文分段有效系数k

    公式: real_afp = k × A + B
    其中:
      A = Σ C_m × input_tokens_m / 10000  (C_m=模型基础系数)
      B = Σ C_m × output_tokens_m / 10000
      k = (real_afp - B) / A

    k含义: 有效分段倍率 (0.67=全≤32k, 1=全32k-128k, 2=全>128k)

    Returns:
        dict: {'k': float, 'A': float, 'B': float} 或 {'error': str}
    """
    models_config = load_json(models_config_path)
    models = models_config.get('models', {})
    input_ratio = get_real_io_ratio(db_path)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    try:
        c.execute('SELECT model_id, tokens FROM arkcli_model_daily WHERE tokens > 0')
        rows = c.fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {'error': 'arkcli_model_daily表不存在'}

    conn.close()

    A = 0.0  # Σ C_m × input_tokens_m / 10000
    B = 0.0  # Σ C_m × output_tokens_m / 10000

    for model_id, total_tokens in rows:
        if '/' in model_id:
            provider, model_name = model_id.split('/', 1)
        else:
            provider = ''
            model_name = model_id

        config_key, model_info = _find_model_by_provider_and_name(
            models, 'volcengine-agent-plan', model_name)

        if not model_info:
            continue

        pricing = model_info.get('pricing', {})
        billing = model_info.get('billing_type', '')
        if billing != 'afp':
            continue

        tiers = pricing.get('context_tiers', {})
        if not tiers:
            continue

        sorted_tiers = sorted(tiers.keys(), key=lambda x: _parse_tier_lower(x))
        base_tier = sorted_tiers[len(sorted_tiers) // 2]
        base_input_coef = tiers[base_tier]['input_coef']   # C_m (at 1x)
        base_output_coef = tiers[base_tier]['output_coef']  # C_m (constant)

        input_tokens = int(total_tokens * input_ratio)
        output_tokens = total_tokens - input_tokens

        A += base_input_coef * input_tokens / 10000
        B += base_output_coef * output_tokens / 10000

    if A > 0:
        k = (real_total_afp - B) / A
        # 限制在合理范围 [0.67, 2.0]
        k = max(0.67, min(2.0, k))
    else:
        k = 1.0

    return {'k': round(k, 4), 'A': round(A, 2), 'B': round(B, 2)}


def _get_arkcli_plan_usage(db_path):
    """从 arkcli_plan_usage 表获取最新套餐用量快照"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    try:
        c.execute('''
            SELECT period_label, used, total, percent
            FROM arkcli_plan_usage
            WHERE collected_at = (SELECT MAX(collected_at) FROM arkcli_plan_usage)
        ''')
        rows = c.fetchall()
        data = {}
        for row in rows:
            label, used, total, percent = row
            data[label] = {'used': used, 'total': total, 'percent': percent}
        conn.close()
        return data
    except sqlite3.OperationalError:
        conn.close()
        return {}


def check_plan_limits(db_path, plans_config):
    """检查套餐三层限额消耗情况。
    plans_config 可以是 dict 或文件路径(str)。
    优先使用 arkcli 套餐层面真实数据，无 arkcli 数据时回退到 api_calls 表。
    """
    if isinstance(plans_config, str):
        plans = load_json(plans_config)
    else:
        plans = plans_config
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    arkcli_data = _get_arkcli_plan_usage(db_path)
    now = datetime.now()
    results = {}

    for plan_key, plan in plans.get('plans', {}).items():
        limits = plan.get('limits', {})
        result = {}

        # 月额度
        if 'monthly_afp' in limits:
            limit = limits['monthly_afp']
            if 'monthly' in arkcli_data:
                consumed = arkcli_data['monthly']['used']
                pct = arkcli_data['monthly']['percent']
            else:
                month_start = now.replace(day=1, hour=0, minute=0, second=0)
                c.execute('''SELECT COALESCE(SUM(afp_consumed),0)
                             FROM api_calls
                             WHERE afp_consumed IS NOT NULL
                               AND dt >= ?''',
                          (month_start.strftime('%Y-%m-%d %H:%M:%S'),))
                consumed = c.fetchone()[0]
                pct = (consumed / limit * 100) if limit > 0 else 0
            result['monthly'] = {
                'consumed': round(consumed, 2),
                'limit': limit,
                'pct': round(pct, 1),
            }

        # 周额度
        if 'weekly_afp' in limits:
            limit = limits['weekly_afp']
            if 'weekly' in arkcli_data:
                consumed = arkcli_data['weekly']['used']
                pct = arkcli_data['weekly']['percent']
            else:
                week_start = now - timedelta(days=now.weekday())
                week_start = week_start.replace(hour=0, minute=0, second=0)
                c.execute('''SELECT COALESCE(SUM(afp_consumed),0)
                             FROM api_calls
                             WHERE afp_consumed IS NOT NULL
                               AND dt >= ?''',
                          (week_start.strftime('%Y-%m-%d %H:%M:%S'),))
                consumed = c.fetchone()[0]
                pct = (consumed / limit * 100) if limit > 0 else 0
            result['weekly'] = {
                'consumed': round(consumed, 2),
                'limit': limit,
                'pct': round(pct, 1),
            }

        # 5小时额度
        if 'hourly_5_afp' in limits:
            limit = limits['hourly_5_afp']
            if '5h' in arkcli_data:
                consumed = arkcli_data['5h']['used']
                pct = arkcli_data['5h']['percent']
            else:
                five_hours_ago = (now - timedelta(hours=5)) \
                    .strftime('%Y-%m-%d %H:%M:%S')
                c.execute('''SELECT COALESCE(SUM(afp_consumed),0)
                             FROM api_calls
                             WHERE afp_consumed IS NOT NULL
                               AND dt >= ?''', (five_hours_ago,))
                consumed = c.fetchone()[0]
                pct = (consumed / limit * 100) if limit > 0 else 0
            result['hourly_5'] = {
                'consumed': round(consumed, 2),
                'limit': limit,
                'pct': round(pct, 1),
            }

        results[plan_key] = result

    conn.close()
    return results
