#!/usr/bin/env python3
"""arkcli_provider.py — Ark CLI 数据采集与报告

通过 arkcli 命令行工具获取火山引擎 Agent Plan 的真实 AFP 用量和分模型 token 数据。
解决 OpenClaw 不记录 VP 模型 token 的问题。

依赖（可选）: arkcli 已安装并登录 (arkcli auth login --no-browser)
未安装 arkcli 时，本模块所有功能自动降级，不影响 skill 核心功能。
"""

import json
import subprocess
import sqlite3
import time
import re
import os
import shutil
from datetime import datetime, timezone, timedelta
from calendar import monthrange

# 北京时区
CST = timezone(timedelta(hours=8))

# 模型名映射: arkcli object_name (去日期后缀) → openclaw model_id
MODEL_MAPPING = {
    "deepseek-v4-flash": "volcengine-agent-plan/deepseek-v4-flash",
    "deepseek-v4-pro": "volcengine-agent-plan/deepseek-v4-pro",
    "doubao-seed-2-0-mini": "volcengine-agent-plan/doubao-seed-2.0-mini",
    "doubao-seed-2-0-pro": "volcengine-agent-plan/doubao-seed-2.0-pro",
    "doubao-seed-2-0-code": "volcengine-agent-plan/doubao-seed-2.0-code",
    "doubao-seed-2-0-code-preview": "volcengine-agent-plan/doubao-seed-2.0-code",
    "doubao-seed-2-0-lite": "volcengine-agent-plan/doubao-seed-2.0-lite",
    "kimi-k2.6": "volcengine-agent-plan/kimi-k2.6",
    "kimi-k2.7-code": "volcengine-agent-plan/kimi-k2.7-code",
    "glm-5.1": "volcengine-agent-plan/glm-5.1",
    "glm-5.2": "volcengine-agent-plan/glm-5.2",
    "minimax-m2.7": "volcengine-agent-plan/minimax-m2.7",
    "minimax-m3": "volcengine-agent-plan/minimax-m3",
    "ark-code-latest": "volcengine-agent-plan/ark-code-latest",
}


def is_arkcli_available():
    """检测 arkcli 是否已安装

    Returns:
        bool: True 如果 arkcli 命令可用
    """
    return shutil.which("arkcli") is not None


def get_arkcli_status():
    """获取 arkcli 状态信息（不执行网络请求）

    Returns:
        dict: {"installed": bool, "message": str}
    """
    if not is_arkcli_available():
        return {
            "installed": False,
            "message": "arkcli 未安装。如需 Agent Plan AFP 监控，请执行: npm install -g @volcengine/ark-cli"
        }
    return {
        "installed": True,
        "message": "arkcli 已安装。如未登录请执行: arkcli auth login --no-browser"
    }


def _map_model_name(ark_name):
    """arkcli 模型名 → openclaw model_id"""
    clean = re.sub(r'-\d{6}$', '', ark_name)
    return MODEL_MAPPING.get(clean, f"volcengine-agent-plan/{clean}")


def _run_arkcli(args):
    """执行 arkcli 命令，返回解析后的 JSON

    Raises:
        RuntimeError: arkcli 未安装、未登录或命令失败
    """
    if not is_arkcli_available():
        raise RuntimeError(
            "arkcli 未安装。如需 Agent Plan AFP 监控，请执行: npm install -g @volcengine/ark-cli\n"
            "安装后登录: arkcli auth login --no-browser"
        )

    cmd = ["arkcli"] + args + ["--format", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError("arkcli 命令超时（30秒），请检查网络或重试")

    if result.returncode != 0:
        try:
            err = json.loads(result.stderr or result.stdout)
            msg = err.get("error", {}).get("message", str(err))
        except (json.JSONDecodeError, AttributeError):
            msg = result.stderr or result.stdout

        if "not configured" in str(msg).lower() or "auth" in str(msg).lower():
            raise RuntimeError(
                "arkcli 未登录或会话已过期。请执行: arkcli auth login --no-browser\n"
                "（SSO 会话约 47 小时有效，过期后需重新登录）"
            )
        raise RuntimeError(f"arkcli 命令失败: {msg}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"arkcli 输出解析失败: {result.stdout[:200]}")


def _get_db_path(custom_db=None):
    """获取数据库路径"""
    if custom_db:
        return custom_db
    module_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(module_dir, "data", "history.db")


def _init_tables(conn):
    """创建 arkcli 数据表"""
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS arkcli_plan_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at INTEGER,
            product TEXT,
            edition TEXT,
            tier TEXT,
            period_label TEXT,
            used REAL,
            total REAL,
            percent REAL,
            reset_at INTEGER
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS arkcli_model_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            model_name TEXT,
            model_id TEXT,
            tokens INTEGER,
            billing_type TEXT,
            collected_at INTEGER
        )
    ''')
    conn.commit()


def collect_plan_usage(db_path=None):
    """采集套餐级 AFP 用量快照

    Raises:
        RuntimeError: arkcli 未安装或未登录
    """
    if db_path is None:
        db_path = _get_db_path()

    data = _run_arkcli(["usage", "plan"])
    ts = int(time.time() * 1000)

    conn = sqlite3.connect(db_path)
    _init_tables(conn)
    c = conn.cursor()

    for item in data.get("items", []):
        product = item.get("product", "")
        edition = item.get("edition", "")
        tier = item.get("tier", "")
        for period in item.get("periods", []):
            c.execute('''
                INSERT INTO arkcli_plan_usage
                (collected_at, product, edition, tier, period_label,
                 used, total, percent, reset_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (ts, product, edition, tier, period["label"],
                  period["used"], period["total"], period["percent"],
                  period.get("reset_at", 0)))

    conn.commit()
    conn.close()
    return data


def collect_plan_details(start_date, end_date=None, db_path=None):
    """采集分模型每日 token 用量

    Args:
        start_date: str, "YYYY-MM-DD"
        end_date: str, "YYYY-MM-DD", 默认今天

    Raises:
        RuntimeError: arkcli 未安装或未登录
    """
    if db_path is None:
        db_path = _get_db_path()

    args = ["usage", "plan-details", "--start", start_date]
    if end_date:
        args.extend(["--end", end_date])

    data = _run_arkcli(args)
    ts = int(time.time() * 1000)

    conn = sqlite3.connect(db_path)
    _init_tables(conn)
    c = conn.cursor()

    for detail in data.get("details", []):
        dt = datetime.fromtimestamp(detail["time"] / 1000, tz=CST)
        date_str = dt.strftime("%Y-%m-%d")
        model_name = detail["object_name"]
        model_id = _map_model_name(model_name)

        c.execute('''
            INSERT INTO arkcli_model_daily
            (date, model_name, model_id, tokens, billing_type, collected_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (date_str, model_name, model_id, detail["usage"],
              detail.get("billing_type", ""), ts))

    conn.commit()
    conn.close()
    return data


def collect_all(start_date=None, end_date=None, db_path=None):
    """采集套餐用量 + 分模型明细

    未安装 arkcli 时返回友好提示，不抛异常。
    """
    if not is_arkcli_available():
        return {
            "plan": "skipped: arkcli 未安装 (npm install -g @volcengine/ark-cli)",
            "details": "skipped: arkcli 未安装"
        }

    if start_date is None:
        now = datetime.now(tz=CST)
        start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    results = {}

    # 采集套餐级 AFP
    try:
        collect_plan_usage(db_path)
        results["plan"] = "ok"
    except RuntimeError as e:
        results["plan"] = str(e)

    # 采集分模型明细
    try:
        collect_plan_details(start_date, end_date, db_path)
        results["details"] = f"ok (from {start_date})"
    except RuntimeError as e:
        results["details"] = str(e)

    return results


def _calc_plan_period(monthly_reset_at):
    """根据 monthly reset_at 计算套餐周期

    Returns: (start_dt, end_dt, total_days, elapsed_days, remaining_days)
    """
    if not monthly_reset_at:
        return None

    end_dt = datetime.fromtimestamp(monthly_reset_at / 1000, tz=CST)
    # 套餐起始日 = 到期日往前推一个月
    if end_dt.month == 1:
        start_month = 12
        start_year = end_dt.year - 1
    else:
        start_month = end_dt.month - 1
        start_year = end_dt.year

    start_day = end_dt.day
    max_day = monthrange(start_year, start_month)[1]
    if start_day > max_day:
        start_day = max_day

    start_dt = datetime(start_year, start_month, start_day, 0, 0, 0, tzinfo=CST)

    now = datetime.now(tz=CST)
    total_days = (end_dt - start_dt).days
    elapsed_days = (now - start_dt).days
    remaining_days = (end_dt - now).days

    if elapsed_days < 0:
        elapsed_days = 0
    if remaining_days < 0:
        remaining_days = 0

    return start_dt, end_dt, total_days, elapsed_days, remaining_days


def format_arkcli_report(since=None, db_path=None):
    """生成 arkcli 数据报告（Markdown 格式）

    未安装 arkcli 或无数据时返回提示信息，不抛异常。
    """
    if db_path is None:
        db_path = _get_db_path()

    # arkcli 未安装时的降级提示
    if not is_arkcli_available():
        return (
            "## 📊 Agent Plan 套餐用量（arkcli）\n\n"
            "ℹ️ arkcli 未安装，AFP 套餐监控功能不可用。\n\n"
            "如需启用（火山引擎 Agent Plan 用户推荐）：\n"
            "1. 安装: `npm install -g @volcengine/ark-cli`\n"
            "2. 登录: `arkcli auth login --no-browser`\n"
            "3. 采集: `python3 bin/run.py arkcli collect`\n\n"
            "> 未安装 arkcli 不影响 OpenClaw 缓存数据的采集和费用报告功能。"
        )

    if not os.path.exists(db_path):
        return (
            "## 📊 Agent Plan 套餐用量（arkcli 实时数据）\n\n"
            "⚠️ 未找到数据库，请先执行: `python3 bin/run.py arkcli collect`"
        )

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    lines = []
    lines.append("## 📊 Agent Plan 套餐用量（arkcli 实时数据）\n")

    # === 套餐级 AFP ===
    try:
        c.execute('''
            SELECT product, edition, tier, period_label, used, total, percent, reset_at
            FROM arkcli_plan_usage
            WHERE collected_at = (SELECT MAX(collected_at) FROM arkcli_plan_usage)
            ORDER BY product, period_label
        ''')
        plan_rows = c.fetchall()

        if plan_rows:
            plans = {}
            for row in plan_rows:
                key = (row[0], row[1], row[2])
                if key not in plans:
                    plans[key] = []
                plans[key].append({
                    "label": row[3], "used": row[4], "total": row[5],
                    "percent": row[6], "reset_at": row[7]
                })

            period_labels = {"5h": "5小时", "weekly": "每周", "monthly": "每月"}

            for (product, edition, tier), periods in plans.items():
                tier_name = tier.upper() if tier else ""
                ed_name = f" ({edition})" if edition else ""
                lines.append(f"### {product} - {tier_name} 套餐{ed_name}\n")

                # 套餐周期信息
                monthly_period = next((p for p in periods if p["label"] == "monthly"), None)
                period_info = _calc_plan_period(monthly_period["reset_at"]) if monthly_period else None

                if period_info:
                    start_dt, end_dt, total_days, elapsed_days, remaining_days = period_info
                    lines.append(f"**套餐周期**: {start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')} "
                                 f"（已过 {elapsed_days} 天 / 剩余 {remaining_days} 天 / 共 {total_days} 天）\n")

                # 用量表
                lines.append("| 周期 | 已用 AFP | 总额 | 使用率 | 剩余 | 重置时间 |")
                lines.append("|------|----------|------|--------|------|----------|")

                monthly_used = 0
                monthly_total = 0

                for p in periods:
                    label = period_labels.get(p["label"], p["label"])
                    used = p["used"]
                    total = p["total"]
                    pct = p["percent"]
                    remaining = total - used

                    if p["label"] == "monthly":
                        monthly_used = used
                        monthly_total = total

                    reset_str = "-"
                    if p["reset_at"]:
                        reset_dt = datetime.fromtimestamp(p["reset_at"] / 1000, tz=CST)
                        reset_str = reset_dt.strftime("%Y-%m-%d %H:%M")

                    warn = " 🔴" if pct >= 80 else (" 🟡" if pct >= 50 else "")
                    lines.append(
                        f"| {label}{warn} | {used:,.1f} | {total:,.0f} | "
                        f"{pct:.1f}% | {remaining:,.1f} | {reset_str} |"
                    )
                lines.append("")

                # 消耗预测
                if period_info and monthly_total > 0 and elapsed_days > 0:
                    start_dt, end_dt, total_days, elapsed_days, remaining_days = period_info
                    daily_avg = monthly_used / elapsed_days
                    projected_total = daily_avg * total_days
                    projected_pct = projected_total / monthly_total * 100

                    expected_usage = monthly_total * (elapsed_days / total_days) if total_days > 0 else 0
                    usage_diff = monthly_used - expected_usage
                    usage_status = "正常" if abs(usage_diff) / monthly_total < 0.1 else (
                        "⚠️ 偏高" if usage_diff > 0 else "✅ 偏低"
                    )

                    lines.append("**消耗预测**:\n")
                    lines.append(f"- 日均消耗: {daily_avg:,.1f} AFP/天")
                    lines.append(f"- 按当前速率预计到期消耗: {projected_total:,.0f} AFP "
                                 f"（占额度 {projected_pct:.1f}%）")
                    lines.append(f"- 时间进度预期用量: {expected_usage:,.0f} AFP")
                    lines.append(f"- 实际 vs 预期: {'+' if usage_diff >= 0 else ''}{usage_diff:,.0f} AFP "
                                 f"→ {usage_status}")

                    if projected_total > monthly_total:
                        overflow = projected_total - monthly_total
                        overflow_days = int(overflow / daily_avg) if daily_avg > 0 else 0
                        lines.append(f"- 🔴 **预警**: 按当前速率将超支 {overflow:,.0f} AFP，"
                                     f"预计提前 {overflow_days} 天耗尽月度额度")
                    elif remaining_days > 0:
                        safe_daily = (monthly_total - monthly_used) / remaining_days
                        lines.append(f"- ✅ 剩余可日均消耗: {safe_daily:,.1f} AFP/天（不超支的安全线）")

                    lines.append("")

        else:
            lines.append("⚠️ 无套餐用量数据，请先执行: `python3 bin/run.py arkcli collect`\n")

    except sqlite3.OperationalError:
        lines.append("⚠️ 无套餐用量数据，请先执行: `python3 bin/run.py arkcli collect`\n")

    # === 分模型每日 token（显示全部已采集数据，不受 since 过滤）===
    try:
        query = '''
            SELECT date, model_id, model_name,
                   SUM(tokens) as total_tokens,
                   SUM(COALESCE(afp_consumed, 0)) as total_afp
            FROM arkcli_model_daily
            GROUP BY date, model_id ORDER BY date DESC, total_tokens DESC
        '''

        c.execute(query)
        model_rows = c.fetchall()

        if model_rows:
            has_afp = any(row[4] and row[4] > 0 for row in model_rows)
            lines.append("### 分模型每日 Token 用量（arkcli）\n")
            if has_afp:
                lines.append("| 日期 | 模型 | Token 总量 | AFP 消耗 |")
                lines.append("|------|------|-----------|----------|")
            else:
                lines.append("| 日期 | 模型 | Token 总量 |")
                lines.append("|------|------|-----------|")

            for row in model_rows:
                date_str = row[0]
                model_id = row[1] or row[2]
                tokens = row[3]
                afp_val = row[4] or 0
                if has_afp:
                    lines.append(f"| {date_str} | {model_id} | {tokens:,} | {afp_val:,.1f} |")
                else:
                    lines.append(f"| {date_str} | {model_id} | {tokens:,} |")

            lines.append("")

            # 模型汇总（显示全部已采集数据，不受 since 过滤）
            summary_query = '''
                SELECT model_id, model_name,
                       SUM(tokens) as total_tokens,
                       SUM(COALESCE(afp_consumed, 0)) as total_afp
                FROM arkcli_model_daily
                GROUP BY model_id ORDER BY total_tokens DESC
            '''

            c.execute(summary_query)
            summary_rows = c.fetchall()

            if summary_rows:
                lines.append("### 模型用量汇总\n")
                grand_total = sum(r[2] for r in summary_rows)
                grand_afp = sum(r[3] or 0 for r in summary_rows)
                if has_afp:
                    lines.append("| 模型 | Token 总量 | 占比 | AFP 消耗 |")
                    lines.append("|------|-----------|------|----------|")
                    for row in summary_rows:
                        model_id = row[0] or row[1]
                        tokens = row[2]
                        afp_val = row[3] or 0
                        pct = tokens / grand_total * 100 if grand_total else 0
                        lines.append(f"| {model_id} | {tokens:,} | {pct:.1f}% | {afp_val:,.1f} |")
                    lines.append(f"| **合计** | **{grand_total:,}** | **100%** | **{grand_afp:,.1f}** |")
                else:
                    lines.append("| 模型 | Token 总量 | 占比 |")
                    lines.append("|------|-----------|------|")
                    for row in summary_rows:
                        model_id = row[0] or row[1]
                        tokens = row[2]
                        pct = tokens / grand_total * 100 if grand_total else 0
                        lines.append(f"| {model_id} | {tokens:,} | {pct:.1f}% |")
                    lines.append(f"| **合计** | **{grand_total:,}** | **100%** |")
                lines.append("")

    except sqlite3.OperationalError:
        pass

    conn.close()
    return "\n".join(lines)
