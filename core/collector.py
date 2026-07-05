"""collector.py — 数据采集模块
从 ~/.openclaw/agents/*/sessions/.usage-cost-cache.json 读取调用记录，
经格式校验、别名映射后增量写入 SQLite history.db。
"""

import json
import os
import glob
import sqlite3
from datetime import datetime


def init_db(db_path):
    """初始化 SQLite 数据库，创建所需表结构"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS api_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp_ms INTEGER NOT NULL,
        dt TEXT NOT NULL,
        agent TEXT NOT NULL,
        provider TEXT NOT NULL,
        model_raw TEXT NOT NULL,
        model_id TEXT NOT NULL,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        cost_yuan REAL,
        afp_consumed REAL,
        is_unknown INTEGER DEFAULT 0,
        task_type TEXT DEFAULT NULL,
        UNIQUE(timestamp_ms, agent, provider, model_raw)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS collect_cursor (
        agent TEXT PRIMARY KEY,
        last_timestamp_ms INTEGER DEFAULT 0,
        last_collected_at TEXT DEFAULT (datetime('now', 'localtime'))
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_summary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        agent TEXT NOT NULL,
        provider TEXT NOT NULL,
        model_id TEXT NOT NULL,
        task_type TEXT,
        calls INTEGER DEFAULT 0,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        cost_yuan REAL DEFAULT 0,
        afp_consumed REAL DEFAULT 0,
        UNIQUE(date, agent, provider, model_id, task_type)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS plan_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date TEXT NOT NULL,
        plan_name TEXT NOT NULL,
        limit_type TEXT NOT NULL,
        total_limit REAL,
        consumed REAL,
        remaining REAL,
        remaining_pct REAL,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS schema_version (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now', 'localtime'))
    )''')
    conn.commit()
    return conn


def load_models_config(config_path):
    """加载 openclaw.models.json"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def build_alias_map(models_config):
    """构建 (provider, model_raw) → model_id 的 provider-aware 映射表
    同一模型名在不同供应商下自然映射到不同配置条目，无需 provider_overrides。
    """
    alias_map = {}
    for model_id, info in models_config.get('models', {}).items():
        provider = info.get('provider', '')
        # 注册 model_id 本身（处理 model_raw == model_id 的情况）
        alias_map[(provider, model_id)] = model_id
        # 注册该条目下的所有别名
        for alias in info.get('aliases', []):
            alias_map[(provider, alias)] = model_id
    return alias_map


def find_cache_files(base_path='~/.openclaw', agent_filter=None):
    """查找所有 agent 的缓存文件，支持按 agent 过滤"""
    bp = os.path.normpath(os.path.expanduser(base_path))
    if agent_filter and agent_filter != '*':
        pattern = os.path.join(bp, 'agents', agent_filter,
                               'sessions', '.usage-cost-cache.json')
        path = os.path.expanduser(pattern)
        if os.path.exists(path):
            return [(agent_filter, path)]
        return []
    pattern = os.path.join(bp, 'agents', '*', 'sessions',
                           '.usage-cost-cache.json')
    files = []
    for path in glob.glob(pattern):
        # 从路径中提取 agent 名（兼容 Linux/Windows 路径分隔符）
        normalized = path.replace('\\', '/')
        parts = normalized.split('/')
        idx = parts.index('agents')
        agent_name = parts[idx + 1]
        files.append((agent_name, path))
    return sorted(files)


def validate_cache_format(data):
    """校验缓存文件格式是否合法，返回 (bool, error_msg)"""
    if isinstance(data, list):
        # 旧版数组格式
        required = {'timestamp', 'provider', 'model', 'usageTotals'}
        usage_required = {'input', 'output', 'totalTokens'}
        for i, rec in enumerate(data):
            if not isinstance(rec, dict):
                return False, f"第{i+1}条记录不是有效对象"
            missing = required - set(rec.keys())
            if missing:
                return False, f"第{i+1}条记录缺少字段: {missing}"
            ut = rec.get('usageTotals', {})
            if not isinstance(ut, dict) or not usage_required.issubset(ut.keys()):
                return False, f"第{i+1}条 usageTotals 缺少必要字段"
        return True, ""
    if isinstance(data, dict) and 'files' in data:
        # v4 对象格式
        return True, ""
    return False, "无法识别的缓存文件格式"


def _parse_entry(agent, entry, alias_map):
    """将 v4 usageEntry 解析为标准格式"""
    ts = entry['timestamp']
    # 使用系统本地时间，与 OpenClaw 缓存中的时间保持一致
    dt = datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M:%S')
    model_raw = entry['model']
    provider = entry['provider']
    # 用 (provider, model_raw) 组合查找配置键，存储为 "provider/model_id"
    config_key = alias_map.get((provider, model_raw))
    if config_key:
        model_id = f"{provider}/{config_key}"
        is_unknown = 0
    else:
        model_id = f"{provider}/{model_raw}"
        is_unknown = 1
    return {
        'timestamp_ms': ts, 'dt': dt, 'agent': agent,
        'provider': provider, 'model_raw': model_raw,
        'model_id': model_id,
        'input_tokens': entry.get('input', 0),
        'output_tokens': entry.get('output', 0),
        'cache_read_tokens': entry.get('cacheRead', 0),
        'total_tokens': entry.get('totalTokens', 0),
        'is_unknown': is_unknown,
    }


def parse_record(agent, record, alias_map):
    """将旧版数组格式记录解析为标准格式（向后兼容）"""
    ts = record['timestamp']
    # 使用系统本地时间，与 OpenClaw 缓存中的时间保持一致
    dt = datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M:%S')
    model_raw = record['model']
    provider = record['provider']
    # 用 (provider, model_raw) 组合查找配置键，存储为 "provider/model_id"
    config_key = alias_map.get((provider, model_raw))
    if config_key:
        model_id = f"{provider}/{config_key}"
        is_unknown = 0
    else:
        model_id = f"{provider}/{model_raw}"
        is_unknown = 1
    ut = record['usageTotals']
    return {
        'timestamp_ms': ts, 'dt': dt, 'agent': agent,
        'provider': provider, 'model_raw': model_raw,
        'model_id': model_id,
        'input_tokens': ut.get('input', 0),
        'output_tokens': ut.get('output', 0),
        'cache_read_tokens': ut.get('cacheRead', 0),
        'total_tokens': ut.get('totalTokens', 0),
        'is_unknown': is_unknown,
    }


def collect(db_path, models_config, base_path='~/.openclaw',
            agent_filter=None, full=False):
    """执行增量采集，返回采集统计信息"""
    conn = init_db(db_path)
    c = conn.cursor()
    alias_map = build_alias_map(models_config)
    cache_files = find_cache_files(base_path, agent_filter)

    if not cache_files:
        conn.close()
        return {'error': '未找到缓存文件'}

    total_inserted = 0
    total_unknown = 0
    agent_stats = {}

    for agent_name, cache_path in cache_files:
        if not os.path.exists(cache_path):
            continue
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            agent_stats[agent_name] = {'error': str(e)}
            continue

        valid, err = validate_cache_format(data)
        if not valid:
            agent_stats[agent_name] = {'error': err}
            continue

        # 提取记录：兼容 v4 对象格式和旧版数组格式
        if isinstance(data, dict) and 'files' in data:
            # v4 格式：从 files.*.usageEntries 中提取所有条目
            raw_entries = []
            for _fp, fdata in data['files'].items():
                if isinstance(fdata, dict):
                    for entry in fdata.get('usageEntries', []):
                        if isinstance(entry, dict) and 'timestamp' in entry:
                            raw_entries.append(entry)
        else:
            raw_entries = data  # 旧版数组格式

        # 获取增量游标
        cursor_row = c.execute(
            "SELECT last_timestamp_ms FROM collect_cursor WHERE agent=?",
            (agent_name,)
        ).fetchone()
        last_ts = 0 if full or not cursor_row else cursor_row[0]

        # 过滤新记录
        new_entries = [r for r in raw_entries if r['timestamp'] > last_ts]
        if not new_entries:
            agent_stats[agent_name] = {'new': 0, 'inserted': 0}
            continue

        # 解析为标准格式
        rows = [_parse_entry(agent_name, e, alias_map)
                for e in new_entries]
        unknown_count = sum(1 for r in rows if r['is_unknown'])

        # 批量插入，UNIQUE 约束自动去重
        c.executemany('''INSERT OR IGNORE INTO api_calls
            (timestamp_ms, dt, agent, provider, model_raw, model_id,
             input_tokens, output_tokens, cache_read_tokens,
             total_tokens, is_unknown)
            VALUES (:timestamp_ms,:dt,:agent,:provider,:model_raw,:model_id,
                    :input_tokens,:output_tokens,:cache_read_tokens,
                    :total_tokens,:is_unknown)''', rows)
        inserted = c.rowcount

        # 更新游标
        max_ts = max(r['timestamp_ms'] for r in rows)
        c.execute('''INSERT OR REPLACE INTO collect_cursor
            (agent, last_timestamp_ms, last_collected_at)
            VALUES (?, ?, datetime('now', 'localtime'))''',
            (agent_name, max_ts))
        conn.commit()

        total_inserted += inserted
        total_unknown += unknown_count
        agent_stats[agent_name] = {
            'new': len(new_entries),
            'inserted': inserted,
            'unknown_models': unknown_count,
        }

    conn.close()
    return {
        'total_inserted': total_inserted,
        'total_unknown': total_unknown,
        'agents': agent_stats,
    }
