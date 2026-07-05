"""test_e2e.py — 端到端冒烟测试
完整走一遍：采集 → 计费 → 报告
"""

import os
import sys
import json
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from core.collector import collect, init_db
from core.calculator import calculate_costs, check_plan_limits
from core.reporter import generate_report


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'test.db')
        self.config_dir = os.path.join(self.tmpdir, 'config')
        self.openclaw_dir = os.path.join(self.tmpdir, '.openclaw')
        os.makedirs(self.config_dir)

        # Create models config
        self.models_config = {
            "models": {
                "qwen3.5-flash": {
                    "display_name": "通义千问3.5 Flash",
                    "provider": "bailian",
                    "billing_type": "pay_as_you_go",
                    "pricing": {
                        "input_per_mtok": 0.20,
                        "output_per_mtok": 2.00,
                        "cache_read_per_mtok": 0.04
                    },
                    "aliases": []
                },
                "doubao-seed-2-0-mini": {
                    "display_name": "豆包 Seed 2.0 Mini",
                    "provider": "ark",
                    "billing_type": "afp",
                    "pricing": {
                        "context_tiers": {
                            "0-32k": {
                                "input_coef": 0.167,
                                "output_coef": 0.25
                            },
                            "32k-128k": {
                                "input_coef": 0.25,
                                "output_coef": 0.25
                            },
                            ">128k": {
                                "input_coef": 0.5,
                                "output_coef": 0.25
                            }
                        }
                    },
                    "aliases": ["doubao-seed-2-0-mini-260428"]
                },
                "unknown-model-x": {
                    "display_name": "Unknown",
                    "provider": "unknown",
                    "billing_type": "unknown",
                    "pricing": {},
                    "aliases": []
                }
            }
        }
        models_path = os.path.join(
            self.config_dir, 'openclaw.models.json')
        with open(models_path, 'w') as f:
            json.dump(self.models_config, f)

        # Create plans config
        self.plans_config = {
            "plans": {
                "afp_medium": {
                    "provider": "ark",
                    "name": "Agent Plan Medium",
                    "price_cny": 200,
                    "limits": {
                        "monthly_afp": 100000,
                        "weekly_afp": 35000,
                        "hourly_5_afp": 10000
                    },
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-30"
                }
            },
            "alert_thresholds": {
                "warning_pct": 80,
                "critical_pct": 90
            }
        }
        plans_path = os.path.join(self.config_dir, 'plans.json')
        with open(plans_path, 'w') as f:
            json.dump(self.plans_config, f)

        # Create fake cache files for 2 agents
        for agent, records in [
            ('main', [
                {
                    "timestamp": 1751200000000,
                    "role": "assistant",
                    "provider": "bailian",
                    "model": "qwen3.5-flash",
                    "usageTotals": {
                        "input": 100000, "output": 5000,
                        "cacheRead": 50000,
                        "totalTokens": 155000, "totalCost": 0.03
                    }
                },
                {
                    "timestamp": 1751200100000,
                    "role": "assistant",
                    "provider": "bailian",
                    "model": "qwen3.5-flash",
                    "usageTotals": {
                        "input": 200000, "output": 10000,
                        "cacheRead": 0,
                        "totalTokens": 210000, "totalCost": 0.24
                    }
                },
                {
                    "timestamp": 1751200200000,
                    "role": "assistant",
                    "provider": "ark",
                    "model": "doubao-seed-2-0-mini-260428",
                    "usageTotals": {
                        "input": 50000, "output": 2000,
                        "cacheRead": 0,
                        "totalTokens": 52000, "totalCost": 0
                    }
                },
            ]),
            ('dev', [
                {
                    "timestamp": 1751200300000,
                    "role": "assistant",
                    "provider": "ark",
                    "model": "some-unknown-model",
                    "usageTotals": {
                        "input": 10000, "output": 500,
                        "cacheRead": 0,
                        "totalTokens": 10500, "totalCost": 0
                    }
                }
            ])
        ]:
            cache_dir = os.path.join(
                self.openclaw_dir, 'agents', agent, 'sessions')
            os.makedirs(cache_dir)
            cache_path = os.path.join(
                cache_dir, '.usage-cost-cache.json')
            with open(cache_path, 'w') as f:
                json.dump(records, f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, onexc=lambda *a: None)

    def test_full_pipeline(self):
        """完整流水线：采集 → 计费 → 报告"""
        # Step 1: Collect
        result = collect(
            self.db_path, self.models_config,
            base_path=self.openclaw_dir, full=True)
        self.assertEqual(result['total_inserted'], 4)
        self.assertEqual(result['total_unknown'], 1)

        # Step 2: Calculate
        models_path = os.path.join(
            self.config_dir, 'openclaw.models.json')
        calc_result = calculate_costs(self.db_path, models_path)
        self.assertEqual(calc_result['calculated'], 3)

        # Step 3: Report (terminal)
        report = generate_report(
            self.db_path, models_path,
            os.path.join(self.config_dir, 'plans.json'),
            since='2025-01-01 00:00:00')
        self.assertIn("LLM Cost Report", report)
        self.assertIn("总览", report)
        self.assertIn("按供应商/模型汇总", report)
        self.assertIn("按 Agent 分布", report)
        self.assertIn("每日趋势", report)
        # 未识别模型应出现在报告中
        self.assertIn("未识别模型", report)

        # Step 4: Report (markdown)
        md_report = generate_report(
            self.db_path, models_path,
            os.path.join(self.config_dir, 'plans.json'),
            since='2025-01-01 00:00:00',
            fmt='markdown')
        self.assertIn("# LLM Cost Report", md_report)
        self.assertIn("| 模型 |", md_report)

    def test_agent_filter(self):
        """测试按 Agent 筛选"""
        collect(self.db_path, self.models_config,
                base_path=self.openclaw_dir, full=True)
        models_path = os.path.join(
            self.config_dir, 'openclaw.models.json')
        calculate_costs(self.db_path, models_path)

        report = generate_report(
            self.db_path, models_path,
            os.path.join(self.config_dir, 'plans.json'),
            since='2025-06-01 00:00:00',
            agent='main')
        # main agent 有 3 条记录
        self.assertIn("main", report)
        # dev agent 不应出现在 Agent 分布中
        self.assertNotIn("dev", report)

    def test_incremental_collect(self):
        """测试增量采集"""
        collect(self.db_path, self.models_config,
                base_path=self.openclaw_dir, full=True)

        # 第二次采集应该没有新数据
        result = collect(
            self.db_path, self.models_config,
            base_path=self.openclaw_dir, full=False)
        self.assertEqual(result['total_inserted'], 0)

    def test_plan_limits(self):
        """测试套餐限额检查"""
        collect(self.db_path, self.models_config,
                base_path=self.openclaw_dir, full=True)
        models_path = os.path.join(
            self.config_dir, 'openclaw.models.json')
        calculate_costs(self.db_path, models_path)

        plans_path = os.path.join(
            self.config_dir, 'plans.json')
        limits = check_plan_limits(self.db_path, plans_path)
        self.assertIn('afp_medium', limits)
        self.assertIn('monthly', limits['afp_medium'])
        self.assertIn('consumed', limits['afp_medium']['monthly'])


if __name__ == '__main__':
    unittest.main()
