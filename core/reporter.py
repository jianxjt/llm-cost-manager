"""reporter.py — 报告生成模块
从 SQLite 读取已计费数据，汇总分析后输出终端表格或 Markdown。
内嵌分析逻辑：按模型/Agent/日期汇总、TOP N、套餐状态检查。
"""

import sqlite3
from datetime import datetime, timedelta
from core.calculator import check_plan_limits, load_json


def _summarize_by_model(cursor, since=None, agent=None):
    """按模型汇总费用和用量，按 provider 排序"""
    query = '''SELECT model_id, provider, is_unknown,
               COUNT(*) as calls,
               SUM(total_tokens) as tokens,
               SUM(COALESCE(cost_yuan, 0)) as cost,
               SUM(COALESCE(afp_consumed, 0)) as afp,
               SUM(input_tokens) as input_tokens,
               SUM(output_tokens) as output_tokens
               FROM api_calls WHERE 1=1'''
    params = []
    if since:
        query += ' AND dt >= ?'
        params.append(since)
    if agent and agent != '*':
        query += ' AND agent = ?'
        params.append(agent)
    query += ''' GROUP BY model_id, provider, is_unknown
                 ORDER BY provider, cost + afp DESC'''
    cursor.execute(query, params)
    return cursor.fetchall()


def _summarize_by_agent(cursor, since=None, agent=None):
    """按 Agent 汇总"""
    query = '''SELECT agent, COUNT(*) as calls,
               SUM(total_tokens) as tokens,
               SUM(COALESCE(cost_yuan, 0)) as cost,
               SUM(COALESCE(afp_consumed, 0)) as afp
               FROM api_calls WHERE 1=1'''
    params = []
    if since:
        query += ' AND dt >= ?'
        params.append(since)
    if agent and agent != '*':
        query += ' AND agent = ?'
        params.append(agent)
    query += ' GROUP BY agent ORDER BY calls DESC'
    cursor.execute(query, params)
    return cursor.fetchall()


def _summarize_by_date(cursor, since=None, agent=None):
    """按日期汇总，支持 since 过滤"""
    query = '''SELECT SUBSTR(dt, 1, 10) as date,
               COUNT(*) as calls,
               SUM(COALESCE(cost_yuan, 0)) as cost,
               SUM(COALESCE(afp_consumed, 0)) as afp
               FROM api_calls WHERE 1=1'''
    params = []
    if since:
        query += ' AND dt >= ?'
        params.append(since)
    if agent and agent != '*':
        query += ' AND agent = ?'
        params.append(agent)
    query += ' GROUP BY SUBSTR(dt, 1, 10) ORDER BY date'
    cursor.execute(query, params)
    return cursor.fetchall()


def _get_overview(cursor, since=None, agent=None):
    """获取总览数据"""
    query = '''SELECT COUNT(*),
               SUM(total_tokens),
               SUM(COALESCE(cost_yuan, 0)),
               SUM(COALESCE(afp_consumed, 0)),
               (SELECT COUNT(DISTINCT model_id) FROM api_calls
                WHERE is_unknown = 1)
               FROM api_calls WHERE 1=1'''
    params = []
    if since:
        query += ' AND dt >= ?'
        params.append(since)
    if agent and agent != '*':
        query += ' AND agent = ?'
        params.append(agent)
    cursor.execute(query, params)
    return cursor.fetchone()


def _fmt_tokens(n):
    if n is None:
        return '0'
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_afp(v):
    return f"{v:,.0f}" if v else "0"


def _fmt_cost(v):
    return f"¥{v:.2f}" if v else "¥0.00"


def _top_model_name(model_id, provider, is_unknown, models_config):
    """生成模型显示名，格式: provider/model_name
    model_id 存储格式为 "provider/config_key"，从中解析出 config_key 查配置。
    """
    if is_unknown:
        return f"未知({model_id})"
    # model_id 格式为 "provider/config_key"，提取 config_key
    if '/' in model_id:
        config_key = model_id.split('/', 1)[1]
    else:
        config_key = model_id
    info = models_config.get('models', {}).get(config_key, {})
    display = info.get('display_name', config_key)
    return f"{provider}/{display}"


_PROVIDER_DISPLAY = {
    'bailian': '阿里百炼',
    'modelstudio': 'ModelStudio',
    'ark': '火山方舟 ARK',
    'volcengine-agent-plan': '火山引擎 Agent Plan',
    'ollama': 'Ollama 本地',
    'openclaw': 'OpenClaw 系统',
}


def _provider_display(provider):
    """provider 键 → 中文显示名"""
    return _PROVIDER_DISPLAY.get(provider, provider)


def _get_pricing_display(model_id, models_config):
    """获取模型定价信息用于显示。model_id 格式为 "provider/config_key"。"""
    config_key = model_id.split('/', 1)[1] if '/' in model_id else model_id
    info = models_config.get('models', {}).get(config_key, {})
    billing = info.get('billing_type', '')
    pricing = info.get('pricing', {})
    if billing == 'pay_as_you_go':
        tiers = pricing.get('context_tiers', {})
        if tiers:
            first_tier = list(tiers.values())[0]
            inp = first_tier.get('input_per_mtok', 0)
            out = first_tier.get('output_per_mtok', 0)
        else:
            inp = pricing.get('input_per_mtok', 0)
            out = pricing.get('output_per_mtok', 0)
        cache = pricing.get('cache_read_per_mtok', 0)
        parts = [f"入{inp}", f"出{out}"]
        if cache:
            parts.append(f"缓存{cache}")
        return ' | '.join(parts) + ' ¥/M'
    elif billing == 'afp':
        tiers = pricing.get('context_tiers', {})
        if tiers:
            first_tier = list(tiers.values())[0]
            ic = first_tier.get('input_coef', 0)
            oc = first_tier.get('output_coef', 0)
            if ic == 0 and oc == 0:
                return '免费（MCP服务）'
            return f"入{ic} 出{oc} AFP系数"
        return 'AFP'
    elif billing == 'free':
        return '免费'
    return '-'


def generate_report(db_path, models_config_path, plans_config_path,
                    since=None, until=None, agent='*', fmt='terminal'):
    """生成费用报告，返回格式化的文本"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    models_config = load_json(models_config_path)
    plans_config = load_json(plans_config_path)

    if not since:
        since = (datetime.now() - timedelta(days=7)) \
            .strftime('%Y-%m-%d %H:%M:%S')

    overview = _get_overview(c, since, agent)
    total_calls, total_tokens, total_cost, total_afp, unknown_count = overview

    if fmt == 'markdown':
        result = _build_markdown(
            db_path, c, models_config, plans_config, since, agent,
            overview)
    else:
        result = _build_terminal(
            db_path, c, models_config, plans_config, since, agent,
            overview)

    conn.close()
    return result


def _build_terminal(db_path, cursor, models_config, plans_config,
                    since, agent, overview):
    """构建终端文本报告"""
    lines = []
    total_calls, total_tokens, total_cost, total_afp, unknown_count = overview

    lines.append("=" * 60)
    lines.append(f"  LLM Cost Report | {since[:10]} ~ 现在")
    lines.append("=" * 60)
    lines.append("")

    # 总览
    lines.append("📊 总览")
    lines.append(f"  阿里百炼费用: {_fmt_cost(total_cost)}（按量后付费）")
    lines.append(f"  AFP总消耗:     {_fmt_afp(total_afp)} 点")
    lines.append(f"  总调用次数:   {total_calls or 0} 次")
    lines.append(f"  总Token数:    {_fmt_tokens(total_tokens)}")
    if unknown_count:
        lines.append(f"  ⚠️ 未识别模型: {unknown_count} 个")
    lines.append("")

    # 套餐状态
    plan_status = check_plan_limits(db_path, plans_config)
    for pk, pv in plan_status.items():
        if 'monthly' in pv:
            m = pv['monthly']
            lines.append(
                f"  火山方舟AFP月额度: {_fmt_afp(m['consumed'])} "
                f"/ {_fmt_afp(m['limit'])} ({m['pct']}%)")
        if 'weekly' in pv:
            w = pv['weekly']
            lines.append(
                f"  火山方舟AFP周额度: {_fmt_afp(w['consumed'])} "
                f"/ {_fmt_afp(w['limit'])} ({w['pct']}%)")
    lines.append("")

    # 按模型汇总（按供应商分组）
    lines.append("💰 按供应商/模型汇总")
    model_rows = _summarize_by_model(cursor, since, agent)
    current_provider = None
    for row in model_rows:
        mid, prov, is_unk, calls, tokens, cost, afp, inp_tok, out_tok = row
        if prov != current_provider:
            current_provider = prov
            lines.append(f"\n  【{_provider_display(prov)}】")
            lines.append(f"  {'模型':<25} {'定价':<20} {'Token':>8} "
                         f"{'费用':>8} {'AFP':>6} {'调用':>6}")
            lines.append("  " + "-" * 78)
        name = _top_model_name(mid, prov, is_unk, models_config)
        pricing_str = _get_pricing_display(mid, models_config)
        cost_str = _fmt_cost(cost) if cost else "¥0.00"
        afp_str = _fmt_afp(afp) if afp else "0"
        lines.append(
            f"  {name:<25} {pricing_str:<20} {_fmt_tokens(tokens):>8} "
            f"{cost_str:>8} {afp_str:>6} {calls:>6}")
    lines.append("")

    # 按 Agent 分布
    lines.append("👤 按 Agent 分布")
    lines.append(f"  {'Agent':<10} {'调用':>6} {'占比':>8} {'费用(¥)':>10} {'AFP':>8}")
    lines.append("  " + "-" * 46)
    for row in _summarize_by_agent(cursor, since, agent):
        ag, calls, tokens, cost, afp = row
        pct = (calls / total_calls * 100) if total_calls else 0
        cost_str = _fmt_cost(cost) if cost else "¥0.00"
        afp_str = _fmt_afp(afp) if afp else "0"
        lines.append(
            f"  {ag:<10} {calls:>6} {pct:>7.1f}% {cost_str:>10} {afp_str:>8}")
    lines.append("")

    # 每日趋势
    lines.append("📈 每日趋势")
    lines.append(f"  {'日期':<12} {'调用':>6} {'费用(¥)':>10} {'AFP':>8}")
    lines.append("  " + "-" * 40)
    for row in _summarize_by_date(cursor, since, agent):
        date, calls, cost, afp = row
        cost_str = _fmt_cost(cost) if cost else "¥0.00"
        afp_str = _fmt_afp(afp) if afp else "0"
        lines.append(f"  {date:<12} {calls:>6} {cost_str:>10} {afp_str:>8}")
    lines.append("")

    return "\n".join(lines)


def _build_markdown(db_path, cursor, models_config, plans_config, since,
                    agent, overview):
    """构建 Markdown 格式报告"""
    lines = []
    total_calls, total_tokens, total_cost, total_afp, unknown_count = overview

    lines.append(f"# LLM Cost Report\n")
    lines.append(f"**时间范围**: {since[:10]} ~ 现在\n")

    lines.append("## 📊 总览\n")
    lines.append(f"| 指标 | 值 |")
    lines.append(f"|------|-----|")
    lines.append(f"| 按量费用 | {_fmt_cost(total_cost)} |")
    lines.append(f"| AFP消耗 | {_fmt_afp(total_afp)} 点 |")
    lines.append(f"| 总调用次数 | {total_calls or 0} |")
    lines.append(f"| 总Token | {_fmt_tokens(total_tokens)} |")
    if unknown_count:
        lines.append(f"| 未识别模型 | {unknown_count} 个 |")
    lines.append("")

    # AFP 套餐状态
    plan_status = check_plan_limits(db_path, plans_config)
    has_afp = any(pv for pv in plan_status.values())
    if has_afp:
        lines.append("## 🔋 AFP 套餐状态\n")
        lines.append("| 周期 | 已消耗 | 额度 | 消耗占比 |")
        lines.append("|------|--------|------|---------|")
        for pk, pv in plan_status.items():
            if 'monthly' in pv:
                m = pv['monthly']
                lines.append(f"| 月度 | {_fmt_afp(m['consumed'])} | {_fmt_afp(m['limit'])} | {m['pct']}% |")
            if 'weekly' in pv:
                w = pv['weekly']
                lines.append(f"| 周度 | {_fmt_afp(w['consumed'])} | {_fmt_afp(w['limit'])} | {w['pct']}% |")
            if 'hourly_5' in pv:
                h = pv['hourly_5']
                lines.append(f"| 5小时 | {_fmt_afp(h['consumed'])} | {_fmt_afp(h['limit'])} | {h['pct']}% |")
        lines.append("")

    # 按模型（按供应商分组）
    lines.append("## 💰 按供应商/模型汇总\n")
    model_rows = _summarize_by_model(cursor, since, agent)
    current_provider = None
    for row in model_rows:
        mid, prov, is_unk, calls, tokens, cost, afp, inp_tok, out_tok = row
        if prov != current_provider:
            current_provider = prov
            lines.append(f"\n### {_provider_display(prov)}\n")
            lines.append("| 模型 | 定价 | Token | 费用(¥) | AFP | 调用次数 |")
            lines.append("|------|------|-------|---------|-----|---------|")
        name = _top_model_name(mid, prov, is_unk, models_config)
        pricing_str = _get_pricing_display(mid, models_config)
        cost_str = _fmt_cost(cost) if cost else "¥0.00"
        afp_str = _fmt_afp(afp) if afp else "0"
        lines.append(
            f"| {name} | {pricing_str} | {_fmt_tokens(tokens)} "
            f"| {cost_str} | {afp_str} | {calls} |")
    lines.append("")

    # 按 Agent
    lines.append("## 👤 按 Agent 分布\n")
    lines.append("| Agent | 调用 | 占比 | 费用(¥) | AFP |")
    lines.append("|-------|------|------|---------|-----|")
    for row in _summarize_by_agent(cursor, since, agent):
        ag, calls, tokens, cost, afp = row
        pct = (calls / total_calls * 100) if total_calls else 0
        cost_str = _fmt_cost(cost) if cost else "¥0.00"
        afp_str = _fmt_afp(afp) if afp else "0"
        lines.append(f"| {ag} | {calls} | {pct:.1f}% | {cost_str} | {afp_str} |")
    lines.append("")

    # 每日趋势
    lines.append("## 📈 每日趋势\n")
    lines.append("| 日期 | 调用 | 费用(¥) | AFP |")
    lines.append("|------|------|---------|-----|")
    for row in _summarize_by_date(cursor, since, agent):
        date, calls, cost, afp = row
        cost_str = _fmt_cost(cost) if cost else "¥0.00"
        afp_str = _fmt_afp(afp) if afp else "0"
        lines.append(f"| {date} | {calls} | {cost_str} | {afp_str} |")

    return "\n".join(lines)

