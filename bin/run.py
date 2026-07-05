#!/usr/bin/env python3
"""run.py — LLM Cost Manager CLI 主入口
用法:
  python run.py collect [--agent AGENT] [--full] [--base-path PATH]
  python run.py report  [--agent AGENT] [--since DATE] [--until DATE]
                         [--format terminal|markdown]
  python run.py config show
  python run.py config set-plan
"""

import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'history.db')
MODELS_CONFIG = os.path.join(CONFIG_DIR, 'openclaw.models.json')
PRICING_CONFIG = os.path.join(CONFIG_DIR, 'pricing.json')
PLANS_CONFIG = os.path.join(CONFIG_DIR, 'plans.json')


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def cmd_collect(args):
    """执行数据采集"""
    from core.collector import collect
    ensure_dirs()
    models_config = _load_json(MODELS_CONFIG)
    base_path = args.base_path if args.base_path else '~/.openclaw'
    result = collect(DB_PATH, models_config,
                     base_path=base_path,
                     agent_filter=args.agent, full=args.full)
    if 'error' in result:
        print(f"❌ 采集失败: {result['error']}")
        sys.exit(1)
    print("✅ 采集完成")
    print(f"   新增记录: {result['total_inserted']} 条")
    if result['total_unknown']:
        print(f"   ⚠️  未识别模型记录: {result['total_unknown']} 条")
    for agent, stats in result.get('agents', {}).items():
        if 'error' in stats:
            print(f"   ❌ {agent}: {stats['error']}")
        else:
            print(f"   {agent}: 新增 {stats.get('inserted', 0)} 条"
                  + (f", 未识别 {stats.get('unknown_models', 0)}"
                     if stats.get('unknown_models') else ""))


def cmd_report(args):
    """生成费用报告"""
    from core.reporter import generate_report
    from core.calculator import calculate_costs
    ensure_dirs()

    # 先对未计费的记录执行计费
    calc_result = calculate_costs(DB_PATH, MODELS_CONFIG)
    if calc_result.get('calculated'):
        print(f"💰 已计算 {calc_result['calculated']} 条新记录的费用")

    since = args.since
    if not since:
        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(days=7)) \
            .strftime('%Y-%m-%d %H:%M:%S')
    else:
        since = f"{since} 00:00:00"

    report = generate_report(
        DB_PATH, MODELS_CONFIG, PLANS_CONFIG,
        since=since, agent=args.agent, fmt=args.format)

    if args.format == 'markdown':
        out_path = os.path.join(DATA_DIR, 'report.md')
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"📄 Markdown 报告已保存: {out_path}")
    else:
        print(report)


def cmd_config(args):
    """配置管理"""
    if args.action == 'show':
        models = _load_json(MODELS_CONFIG)
        pricing = _load_json(PRICING_CONFIG)
        plans = _load_json(PLANS_CONFIG)
        print("📦 已配置模型:")
        for mid, info in models.get('models', {}).items():
            print(f"  - {info['display_name']} ({mid})")
            print(f"    供应商: {info['provider']} | "
                  f"计费: {info['billing_type']}")
        print(f"\n📋 套餐配置:")
        for pk, pv in plans.get('plans', {}).items():
            print(f"  - {pv['name']}: ¥{pv['price_cny']}/月")
            limits = pv.get('limits', {})
            for lk, lv in limits.items():
                print(f"    {lk}: {lv}")
    elif args.action == 'set-plan':
        print("📝 套餐配置编辑:")
        print(f"   请编辑文件: {PLANS_CONFIG}")


def _load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description='LLM Cost Manager — LLM 调用成本管理工具')
    subparsers = parser.add_subparsers(dest='command')

    # collect
    p_collect = subparsers.add_parser('collect', help='采集调用数据')
    p_collect.add_argument('--agent', default='*',
                           help='指定 agent 或 * 全部（默认）')
    p_collect.add_argument('--full', action='store_true',
                           help='全量重采（忽略游标）')
    p_collect.add_argument('--base-path', default=None,
                           help='OpenClaw 数据根目录，默认 ~/.openclaw')

    # report
    p_report = subparsers.add_parser('report', help='生成费用报告')
    p_report.add_argument('--agent', default='*',
                          help='筛选 agent')
    p_report.add_argument('--since', default=None,
                          help='起始日期 YYYY-MM-DD')
    p_report.add_argument('--until', default=None,
                          help='截止日期 YYYY-MM-DD')
    p_report.add_argument('--format', choices=['terminal', 'markdown'],
                          default='terminal', help='输出格式')

    # config
    p_config = subparsers.add_parser('config', help='配置管理')
    p_config.add_argument('action', choices=['show', 'set-plan'],
                          help='配置操作')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        'collect': cmd_collect,
        'report': cmd_report,
        'config': cmd_config,
    }
    commands[args.command](args)


if __name__ == '__main__':
    main()
