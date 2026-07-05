"""test_calculator.py — 计费模块单元测试"""

import os
import sys
import json
import tempfile
import shutil
import sqlite3
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from core.calculator import (
    calc_pay_as_you_go, calc_afp, get_context_tier,
    calculate_costs, check_plan_limits, load_json
)
from core.collector import init_db


class TestCalcPayAsYouGo(unittest.TestCase):
    def test_basic(self):
        pricing = {
            "input_per_mtok": 0.20,
            "output_per_mtok": 2.00,
            "cache_read_per_mtok": 0.04
        }
        # 100000 input * 0.20/1M = 0.02
        # 5000 output * 2.00/1M = 0.01
        # 50000 cache * 0.04/1M = 0.002
        cost = calc_pay_as_you_go(100000, 5000, 50000, pricing)
        self.assertAlmostEqual(cost, 0.032, places=3)

    def test_no_cache(self):
        pricing = {
            "input_per_mtok": 1.00,
            "output_per_mtok": 4.00,
            "cache_read_per_mtok": 0.10
        }
        cost = calc_pay_as_you_go(200000, 10000, 0, pricing)
        # 200000*1.00/1M + 10000*4.00/1M = 0.2 + 0.04 = 0.24
        self.assertAlmostEqual(cost, 0.24, places=4)


class TestCalcAFP(unittest.TestCase):
    def test_basic(self):
        pricing = {
            "context_tiers": {
                "0-32k": {"input_coef": 0.167, "output_coef": 0.25},
                "32k-128k": {"input_coef": 0.25, "output_coef": 0.25},
                ">128k": {"input_coef": 0.5, "output_coef": 0.25}
            }
        }
        # 50000 tokens input → 32k-128k tier → coef 0.25
        # 2000 tokens output → coef 0.25
        # AFP = (50000*0.25 + 2000*0.25) / 10000 = (12500+500)/10000 = 1.3
        afp = calc_afp(50000, 2000, pricing)
        self.assertAlmostEqual(afp, 1.3, places=1)

    def test_small_context(self):
        pricing = {
            "context_tiers": {
                "0-32k": {"input_coef": 0.335, "output_coef": 0.5},
                "32k-128k": {"input_coef": 0.5, "output_coef": 0.5},
                ">128k": {"input_coef": 1.0, "output_coef": 0.5}
            }
        }
        # 30000 tokens input → 0-32k tier → coef 0.335
        # 1000 tokens output → coef 0.5
        # AFP = (30000*0.335 + 1000*0.5) / 10000
        #     = (10050 + 500) / 10000 = 1.055
        afp = calc_afp(30000, 1000, pricing)
        self.assertAlmostEqual(afp, 1.05, places=2)


class TestGetContextTier(unittest.TestCase):
    def test_small(self):
        tiers = {"0-32k": {}, "32k-128k": {}, ">128k": {}}
        self.assertEqual(get_context_tier(10000, tiers), "0-32k")

    def test_medium(self):
        tiers = {"0-32k": {}, "32k-128k": {}, ">128k": {}}
        self.assertEqual(get_context_tier(50000, tiers), "32k-128k")

    def test_large(self):
        tiers = {"0-32k": {}, "32k-128k": {}, ">128k": {}}
        self.assertEqual(get_context_tier(200000, tiers), ">128k")

    def test_boundary_32k(self):
        tiers = {"0-32k": {}, "32k-128k": {}, ">128k": {}}
        self.assertEqual(get_context_tier(32768, tiers), "0-32k")
        self.assertEqual(get_context_tier(32769, tiers), "32k-128k")


class TestCalculateCosts(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'test.db')
        init_db(self.db_path)
        # Insert sample records
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.executemany('''INSERT INTO api_calls
            (timestamp_ms, dt, agent, provider, model_raw, model_id,
             input_tokens, output_tokens, cache_read_tokens,
             total_tokens, is_unknown, cost_yuan, afp_consumed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', [
            (1751200000000, '2025-06-29 10:00:00', 'main',
             'bailian', 'qwen3.5-flash', 'bailian/qwen3.5-flash',
             100000, 5000, 50000, 155000, 0, None, None),
            (1751200100000, '2025-06-29 10:01:00', 'dev',
             'ark', 'doubao-seed-2-0-mini', 'ark/doubao-seed-2-0-mini',
             50000, 2000, 0, 52000, 0, None, None),
        ])
        conn.commit()
        conn.close()

        # Write models config
        self.config_path = os.path.join(self.tmpdir, 'models.json')
        with open(self.config_path, 'w') as f:
            json.dump({
                "models": {
                    "qwen3.5-flash": {
                        "billing_type": "pay_as_you_go",
                        "pricing": {
                            "input_per_mtok": 0.20,
                            "output_per_mtok": 2.00,
                            "cache_read_per_mtok": 0.04
                        }
                    },
                    "doubao-seed-2-0-mini": {
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
                        }
                    }
                }
            }, f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, onexc=lambda *a: None)

    def test_calculate_costs(self):
        result = calculate_costs(self.db_path, self.config_path)
        self.assertEqual(result['calculated'], 2)

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT cost_yuan FROM api_calls WHERE model_id='bailian/qwen3.5-flash'")
        cost = c.fetchone()[0]
        self.assertAlmostEqual(cost, 0.032, places=3)

        c.execute("SELECT afp_consumed FROM api_calls WHERE model_id='ark/doubao-seed-2-0-mini'")
        afp = c.fetchone()[0]
        self.assertIsNotNone(afp)
        conn.close()

    def test_no_double_calculation(self):
        calculate_costs(self.db_path, self.config_path)
        result = calculate_costs(self.db_path, self.config_path)
        self.assertEqual(result['calculated'], 0)


class TestCheckPlanLimits(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, 'test.db')
        init_db(self.db_path)
        self.plans_path = os.path.join(self.tmpdir, 'plans.json')
        with open(self.plans_path, 'w') as f:
            json.dump({
                "plans": {
                    "afp_medium": {
                        "provider": "ark",
                        "name": "Agent Plan Medium",
                        "limits": {
                            "monthly_afp": 100000,
                            "weekly_afp": 35000,
                            "hourly_5_afp": 10000
                        }
                    }
                }
            }, f)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, onexc=lambda *a: None)

    def test_empty_db(self):
        result = check_plan_limits(self.db_path, self.plans_path)
        self.assertEqual(result['afp_medium']['monthly']['consumed'], 0)

    def test_with_data(self):
        from datetime import datetime
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute('''INSERT INTO api_calls
            (timestamp_ms, dt, agent, provider, model_raw, model_id,
             input_tokens, output_tokens, total_tokens,
             is_unknown, afp_consumed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (1751200000000, now, 'main', 'ark',
             'doubao-seed-2-0-mini', 'ark/doubao-seed-2-0-mini',
             50000, 2000, 52000, 0, 1.3))
        conn.commit()
        conn.close()

        result = check_plan_limits(self.db_path, self.plans_path)
        self.assertAlmostEqual(
            result['afp_medium']['monthly']['consumed'], 1.3)


if __name__ == '__main__':
    unittest.main()
