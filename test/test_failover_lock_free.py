"""失败处置路径不得持池锁做网络 I/O。

2026-11-05 04:00 事故：chat() 在 `with self._lock:` 内做候选探活，并把
_check_one_health 提交给线程池等待；探活线程回来要拿同一把池锁
(_guard_pool_outbound/_rotate)，主线程持锁等线程池结束，互等成死锁，
整池所有接口冻结。此用例固化「探活期间锁必须是空的」。
"""
import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest

MODULE_PATH = os.environ.get(
    "API_POOL_MODULE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api_pool_server.py"),
)


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_lockfree_test_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class FailoverLockFreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.module = load_module(self.tmp_dir.name)
        endpoints = [
            self.module.Endpoint(id=f"ep-{i}", name=f"Endpoint {i}", enabled=True, in_pool=True)
            for i in range(3)
        ]
        self.pool = self.module.APIPool(endpoints)
        self.endpoints = endpoints

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_probe_runs_without_holding_pool_lock(self):
        pool = self.pool
        observed = []

        def foreign_thread_can_lock():
            holder = {}

            def worker():
                got = pool._lock.acquire(blocking=False)
                holder["ok"] = got
                if got:
                    pool._lock.release()

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=5)
            return holder.get("ok", False)

        def probe(ep):
            observed.append((ep.id, foreign_thread_can_lock()))
            return False, "probe failed"

        pool._try_endpoint = lambda *a, **k: (None, "连接/超时错误: boom")
        pool._probe_endpoint = probe
        pool._check_one_health = lambda ep: (ep.id, "bad", -1, "explicit failure")

        try:
            pool.chat([{"role": "user", "content": "hi"}])
        except Exception:
            pass

        self.assertTrue(observed, "失败轮转没有走到候选探活，用例前提失效")
        blocked = [ep_id for ep_id, free in observed if not free]
        self.assertEqual(blocked, [], f"探活期间仍持池锁：{blocked}")


if __name__ == "__main__":
    unittest.main()
