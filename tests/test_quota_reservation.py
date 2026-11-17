"""Key 硬额度并发预占回归（审计 H4）：check_quota「请求前读、完成后加」
的竞态允许并发请求共同越过 credit_limit。修复为原子条件 UPDATE 预占 +
收尾幂等释放。覆盖：

1. 20 线程并发预占，成功数与最终 credit_used 都不越限；
2. 释放幂等：重复释放地板 0，不会把额度刷成负数；
3. unlimited key 不做预占；
4. 预占-记账-释放全链路后 credit_used 回到真实积分口径。
"""

import importlib
import threading
import unittest

from admin.db import Base, init_db
from admin.models import ApiKey, ModelConfig
from admin.security import hash_key, reserve_quota

proxy = importlib.import_module("admin.routers.proxy")


class QuotaReservationTest(unittest.TestCase):
    def setUp(self):
        self.engine = proxy.database.engine if hasattr(proxy, "database") else None
        from admin import db as database
        self.database = database
        Base.metadata.drop_all(database.engine)
        Base.metadata.create_all(database.engine)
        init_db()
        self.db = database.SessionLocal()
        self.db.add(ModelConfig(model_id="m1", credit_multiplier=2))
        self.db.commit()
        self.key = ApiKey(name="k", key_hash=hash_key("k-key"),
                          unlimited=0, credit_limit=10, credit_used=0)
        self.db.add(self.key)
        self.db.commit()
        self.kid = self.key.id

    def tearDown(self):
        self.db.close()

    def _fresh(self):
        s = self.database.SessionLocal()
        try:
            return s.query(ApiKey).get(self.kid)
        finally:
            s.close()

    def _used(self):
        s = self.database.SessionLocal()
        try:
            return s.query(ApiKey).get(self.kid).credit_used
        finally:
            s.close()

    def test_concurrent_reservation_never_overshoots(self):
        """每笔预占 3，限额 10：20 线程并发入场，成功数 ≤ 4 且不越限。"""
        barrier = threading.Barrier(20)
        ok = []

        def worker():
            s = self.database.SessionLocal()
            try:
                barrier.wait()
                k = s.query(ApiKey).get(self.kid)
                if reserve_quota(s, k, 3.0):
                    ok.append(1)
            finally:
                s.close()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(len(ok), 4, f"放行 {len(ok)} 笔，共同越限")
        self.assertGreaterEqual(len(ok), 1)
        self.assertLessEqual(self._used(), 10)

    def test_release_floors_at_zero(self):
        k = self._fresh()
        self.assertTrue(reserve_quota(self.db, k, 5.0))
        self.assertAlmostEqual(self._used(), 5.0)
        proxy._release_quota(self.kid, 5.0)
        proxy._release_quota(self.kid, 5.0)  # 重复释放：地板 0
        self.assertAlmostEqual(self._used(), 0.0)

    def test_unlimited_key_skips_reservation(self):
        s = self.database.SessionLocal()
        try:
            uk = ApiKey(name="u", key_hash=hash_key("u-key"), unlimited=1,
                        credit_limit=1, credit_used=0)
            s.add(uk)
            s.commit()
            self.assertTrue(reserve_quota(s, uk, 999.0))
        finally:
            s.close()
        s2 = self.database.SessionLocal()
        try:
            self.assertAlmostEqual(s2.query(ApiKey).get(uk.id).credit_used, 0.0)
        finally:
            s2.close()

    def test_reserve_then_settle_then_release_matches_real_usage(self):
        """预占 6 → 记账真实 2 → 释放预占 6：终值等于真实积分 2。"""
        k = self._fresh()
        self.assertTrue(reserve_quota(self.db, k, 6.0))
        # 模拟 _record_usage 的累加口径
        from sqlalchemy import func
        self.db.query(ApiKey).filter(ApiKey.id == self.kid).update(
            {ApiKey.credit_used: func.coalesce(ApiKey.credit_used, 0) + 2},
            synchronize_session=False)
        self.db.commit()
        proxy._release_quota(self.kid, 6.0)
        self.assertAlmostEqual(self._used(), 2.0)

    def test_estimate_helper(self):
        est = proxy._estimate_request_credits(self.db, "m1", {"messages": ["x" * 400], "max_tokens": 100})
        # prompt 400+ 字符 → ~100 token；(≈105+100)*2/1000 ≈ 0.41
        self.assertGreaterEqual(est, 0.40)
        self.assertLessEqual(est, 0.45)
        # max_tokens 封顶 8192
        est2 = proxy._estimate_request_credits(self.db, "m1", {"messages": ["x" * 400], "max_tokens": 999999})
        self.assertLessEqual(est2, (120 + 8192) * 2 / 1000.0 + 1e-9)
        # 未知模型倍率 → 0（不预占，与 _record_usage 的 0 记账口径一致）
        self.assertAlmostEqual(proxy._estimate_request_credits(self.db, "nope", {"a": 1}), 0.0)


if __name__ == "__main__":
    unittest.main()
