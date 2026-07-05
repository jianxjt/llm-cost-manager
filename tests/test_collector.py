"""test_collector.py — 采集模块单元测试"""

import os
import sys
import json
import tempfile
import shutil
import sqlite3
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from core.collector import (
    init_db, validate_cache_format, parse_record,
    build_alias_map, collect, find_cache_files
)


class TestValidateCacheFormat(unittest.TestCase):
    def test_valid_data(self):
        data = [{
            "timestamp": 1751200000000,
            "provider": "bailian",
            "model": "qwen3.5-flash",
            "usageTotals": {
                "input": 100, "output": 50,
                "totalTokens": 150
            }
        }]
        ok, err = validate_cache_format(data)
        self.assertTrue(ok)

    def test_missing_field(self):
        data = [{"timestamp": 123, "provider": "x"}]
        ok, err = validate_cache_format(data)
        self.assertFalse(ok)
        self.assertIn("缺少字段", err)

    def test_empty_list(self):
        ok, err = validate_cache_format([])
        self.assertTrue(ok)

    def test_not_a_list(self):
        ok, err = validate_cache_format({"key": "val"})
        self.assertFalse(ok)


class TestBuildAliasMap(unittest.TestCase):
    def test_alias_map(self):
        config = {
            "models": {
                "qwen3.5-flash": {
                    "provider": "bailian",
                    "aliases": ["qwen3.5-flash-260501"]
                },
                "deepseek-v4": {
                    "provider": "bailian",
                    "aliases": []
                }
            }
        }
        am = build_alias_map(config)
        self.assertEqual(am[("bailian", "qwen3.5-flash")],
                         "qwen3.5-flash")
        self.assertEqual(am[("bailian", "qwen3.5-flash-260501")],
                         "qwen3.5-flash")
        self.assertEqual(am[("bailian", "deepseek-v4")],
                         "deepseek-v4")
        self.assertNotIn(("bailian", "unknown-model"), am)

    def test_provider_scoped_matching(self):
        """同名模型在不同供应商下映射到不同配置条目"""
        config = {
            "models": {
                "qwen3.5-flash": {
                    "provider": "bailian",
                    "aliases": []
                },
                "qwen3.5-flash-ms": {
                    "provider": "modelstudio",
                    "aliases": ["qwen3.5-flash"]
                }
            }
        }
        am = build_alias_map(config)
        # 同一 model_raw 在不同 provider 下映射到不同条目
        self.assertEqual(am[("bailian", "qwen3.5-flash")],
                         "qwen3.5-flash")
        self.assertEqual(am[("modelstudio", "qwen3.5-flash")],
                         "qwen3.5-flash-ms")


class TestParseRecord(unittest.TestCase):
    def test_parse_known_model(self):
        record = {
            "timestamp": 1751200000000,
            "provider": "bailian",
            "model": "qwen3.5-flash",
            "usageTotals": {
                "input": 100, "output": 50,
                "cacheRead": 10, "totalTokens": 160
            }
        }
        alias_map = {("bailian", "qwen3.5-flash"): "qwen3.5-flash"}
        result = parse_record("main", record, alias_map)
        self.assertEqual(result['agent'], 'main')
        self.assertEqual(result['model_id'], 'bailian/qwen3.5-flash')
        self.assertEqual(result['is_unknown'], 0)
        self.assertEqual(result['input_tokens'], 100)

    def test_parse_unknown_model(self):
        record = {
            "timestamp": 1751200000000,
            "provider": "bailian",
            "model": "new-model-xyz",
            "usageTotals": {
                "input": 100, "output": 50,
                "cacheRead": 0, "totalTokens": 150
            }
        }
        result = parse_record("dev", record, {})
        self.assertEqual(result['is_unknown'], 1)
        self.assertEqual(result['model_id'], 'bailian/new-model-xyz')

    def test_parse_provider_scoped(self):
        """同一 model_raw 在不同 provider 下解析到不同 model_id"""
        record_ms = {
            "timestamp": 1751200000000,
            "provider": "modelstudio",
            "model": "qwen3.5-flash",
            "usageTotals": {
                "input": 100, "output": 50,
                "cacheRead": 0, "totalTokens": 150
            }
        }
        alias_map = {
            ("bailian", "qwen3.5-flash"): "qwen3.5-flash",
            ("modelstudio", "qwen3.5-flash"): "qwen3.5-flash-ms",
        }
        result = parse_record("main", record_ms, alias_map)
        self.assertEqual(result['model_id'], 'modelstudio/qwen3.5-flash-ms')
        self.assertEqual(result['is_unknown'], 0)
        # 同 model_raw 在 bailian 下映射到不同条目
        record_bl = dict(record_ms, provider="bailian")
        result_bl = parse_record("main", record_bl, alias_map)
        self.assertEqual(result_bl['model_id'], 'bailian/qwen3.5-flash')


class TestCollectEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'test.db')
        self.openclaw_dir = os.path.join(self.tmpdir, '.openclaw')
        # Create fake cache files
        for agent, fixture in [('main', 'sample_bailian.json'),
                                ('dev', 'sample_ark.json')]:
            cache_dir = os.path.join(
                self.openclaw_dir, 'agents', agent, 'sessions')
            os.makedirs(cache_dir)
            src = os.path.join(
                os.path.dirname(__file__), 'fixtures', fixture)
            dst = os.path.join(cache_dir, '.usage-cost-cache.json')
            shutil.copy(src, dst)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, onexc=lambda *a: None)

    def test_full_collect(self):
        config = {
            "models": {
                "qwen3.5-flash": {
                    "provider": "bailian", "aliases": []},
                "deepseek-v4": {
                    "provider": "bailian", "aliases": []},
                "doubao-seed-2-0-mini": {
                    "provider": "ark", "aliases": []},
                "deepseek-v4-flash": {
                    "provider": "ark", "aliases": []}
            }
        }
        result = collect(self.db_path, config,
                         base_path=self.openclaw_dir, full=True)
        self.assertEqual(result['total_inserted'], 4)
        self.assertEqual(result['total_unknown'], 0)
        # Verify DB contents
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM api_calls")
        self.assertEqual(c.fetchone()[0], 4)
        c.execute("SELECT COUNT(*) FROM collect_cursor")
        self.assertEqual(c.fetchone()[0], 2)  # 2 agents
        conn.close()

    def test_incremental_collect(self):
        config = {
            "models": {
                "qwen3.5-flash": {
                    "provider": "bailian", "aliases": []},
                "deepseek-v4": {
                    "provider": "bailian", "aliases": []},
                "doubao-seed-2-0-mini": {
                    "provider": "ark", "aliases": []},
                "deepseek-v4-flash": {
                    "provider": "ark", "aliases": []}
            }
        }
        # First collect
        collect(self.db_path, config,
                base_path=self.openclaw_dir, full=True)
        # Second collect - should insert 0 new records
        result = collect(self.db_path, config,
                         base_path=self.openclaw_dir, full=False)
        self.assertEqual(result['total_inserted'], 0)

    def test_agent_filter(self):
        config = {
            "models": {
                "qwen3.5-flash": {
                    "provider": "bailian", "aliases": []},
                "deepseek-v4": {
                    "provider": "bailian", "aliases": []}
            }
        }
        result = collect(self.db_path, config,
                         base_path=self.openclaw_dir,
                         agent_filter='main', full=True)
        self.assertEqual(result['total_inserted'], 2)
        self.assertIn('main', result['agents'])
        self.assertNotIn('dev', result['agents'])


if __name__ == '__main__':
    unittest.main()
