import unittest

from skills_workspace.retail_acceptance import run


class RetailAcceptanceTest(unittest.TestCase):
    def test_offline_retail_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        # 高优先级晚到承诺排在候选首位，打烊后到达按营业日结转。
        self.assertEqual(["p-high", "p-win", "p-low", "p-late"], result["candidate_order"])
        self.assertEqual("2026-09-28T01:00:00Z", result["carried_forward"]["effective_at"])
        self.assertEqual("2026-09-28", result["carried_forward"]["business_date"])
        # 过期批次与保质窗口在方案中可见。
        self.assertTrue(result["plan_one"]["expired_skipped"])
        self.assertTrue(result["plan_one"]["window_filtered"])
        self.assertEqual(25, result["plan_one"]["p-late_shortfall"])
        # 过期方案整体冲突且不留部分扣减。
        self.assertTrue(result["stale_confirm_conflict"])
        self.assertEqual(25, result["stale_plan_allocated"])
        self.assertTrue(result["no_partial_deduction"])
        # 取消只释放未履约数量。
        self.assertEqual({"fulfilled": 2, "cancelled": 4, "status": "closed"},
                         result["p_high_final"])
        # 解释视图包含完整计算与审计依据。
        self.assertEqual("gained", result["explain"]["outcome"])
        self.assertGreaterEqual(result["explain"]["audit_events"], 3)


if __name__ == "__main__":
    unittest.main()
