"""用 fakeredis 在无 Redis 环境下验证 RedisStore / 熔断 / 限流的真实 Redis 语义，
以及 Edge TTS 适配器（mock 网络）。"""
import threading
import time
from unittest.mock import MagicMock, patch

import fakeredis
from django.test import SimpleTestCase

from apps.ai_gateway.circuit_breaker import CircuitBreaker, CLOSED, HALF_OPEN, OPEN
from apps.ai_gateway.rate_limiter import RateLimiter
from apps.ai_gateway.redis_client import MemoryStore, RedisStore


def _fake_store():
    server = fakeredis.FakeServer()
    return RedisStore(client=fakeredis.FakeRedis(server=server, decode_responses=True))


class StoreContractMixin:
    """MemoryStore 与 RedisStore 必须表现一致。"""

    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()

    def test_basic_ops(self):
        s = self.store
        self.assertIsNone(s.get("k"))
        self.assertEqual(s.ttl("k"), -2)
        s.set("k", 5)
        self.assertEqual(s.get("k"), "5")  # 均返回 str
        self.assertEqual(s.ttl("k"), -1)
        self.assertEqual(s.incr("k"), 6)
        self.assertTrue(s.expire("k", 30))
        self.assertGreater(s.ttl("k"), 0)
        self.assertEqual(s.incr("k"), 7)
        self.assertGreater(s.ttl("k"), 0)  # INCR 保留 TTL
        s.delete("k")
        self.assertFalse(s.exists("k"))

    def test_set_nx(self):
        self.assertTrue(self.store.set_nx("p", "1", ex=10))
        self.assertFalse(self.store.set_nx("p", "1", ex=10))
        self.store.delete("p")
        self.assertTrue(self.store.set_nx("p", "1", ex=10))

    def test_zset_ops(self):
        """ZSET 四方法双实现一致（并发限流租约）。"""
        s = self.store
        self.assertEqual(s.zcard("z"), 0)
        self.assertEqual(s.zadd("z", "a", 100), 1)
        self.assertEqual(s.zadd("z", "b", 200), 1)
        self.assertEqual(s.zadd("z", "a", 300), 0)  # 覆盖不新增（a: 100→300）
        self.assertEqual(s.zcard("z"), 2)
        # 范围删（score ∈ [min, max]，闭区间）：a=300 不在 [0,150]，b=200 不在
        self.assertEqual(s.zremrangebyscore("z", 0, 150), 0)
        self.assertEqual(s.zremrangebyscore("z", 0, 250), 1)  # b=200 命中
        self.assertEqual(s.zcard("z"), 1)
        self.assertEqual(s.zrem("z", "a"), 1)
        self.assertEqual(s.zrem("z", "a"), 0)  # 幂等
        self.assertEqual(s.zcard("z"), 0)
        # 空 key 上的操作不报错
        self.assertEqual(s.zremrangebyscore("nope", 0, 100), 0)
        self.assertEqual(s.zrem("nope", "x"), 0)

    def test_rpm_fixed_window(self):
        rl = RateLimiter(store=self.store)
        self.assertIsNotNone(rl.try_acquire(1, max_rpm=2))
        ttl_first = self.store.ttl("rl:1:rpm")
        self.assertIsNotNone(rl.try_acquire(1, max_rpm=2))
        self.assertIsNone(rl.try_acquire(1, max_rpm=2))
        self.assertLessEqual(self.store.ttl("rl:1:rpm"), ttl_first)
        self.assertEqual(rl.current_rpm(1), 2)

    def test_release_stale_token_no_harm(self):
        """未占用/租约已换代时 release 是 no-op，不产生负计数。"""
        rl = RateLimiter(store=self.store)
        rl.release(9, "9:stale")  # 未占用就释放（如租约已过期回收）
        self.assertEqual(rl.current_concurrent(9), 0)
        token = rl.try_acquire(9, max_concurrent=1)
        self.assertIsNotNone(token)
        self.assertIsNone(rl.try_acquire(9, max_concurrent=1))
        rl.release(9, "9:other")  # 非持有者 token：no-op
        self.assertEqual(rl.current_concurrent(9), 1)
        rl.release(9, token)
        self.assertEqual(rl.current_concurrent(9), 0)
        self.assertIsNotNone(rl.try_acquire(9, max_concurrent=1))

    def test_breaker_cycle(self):
        cb = CircuitBreaker(store=self.store, cooldown_seconds=30, max_cooldown_seconds=100)
        for _ in range(5):
            cb.record_failure(1)
        self.assertEqual(cb.state(1), OPEN)
        self.assertFalse(cb.is_available(1))
        self.store.set("cb:1:cooldown_until", "0")
        self.assertTrue(cb.is_available(1))
        self.assertEqual(cb.state(1), OPEN)  # 只读检查不改状态
        self.assertTrue(cb.allow_request(1))
        self.assertFalse(cb.allow_request(1))  # 唯一探测名额
        self.assertEqual(cb.state(1), HALF_OPEN)
        cb.record_failure(1)
        self.assertEqual(cb.current_cooldown(1), 60)
        self.store.set("cb:1:cooldown_until", "0")
        self.assertTrue(cb.allow_request(1))
        cb.release_probe(1)  # 被限流未实际调用 -> 归还
        self.assertTrue(cb.allow_request(1))
        cb.record_success(1)
        self.assertEqual(cb.state(1), CLOSED)
        self.assertEqual(cb.consecutive_errors(1), 0)


class MemoryStoreContractTests(StoreContractMixin, SimpleTestCase):
    def make_store(self):
        return MemoryStore()


class RedisStoreContractTests(StoreContractMixin, SimpleTestCase):
    def make_store(self):
        return _fake_store()

    def test_concurrent_acquire_respects_limit(self):
        store = self.store
        results, barrier = [], threading.Barrier(20)

        def worker():
            rl = RateLimiter(store=store)
            barrier.wait()
            results.append(rl.try_acquire(5, max_rpm=100, max_concurrent=3) is not None)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 3)
        self.assertEqual(RateLimiter(store=store).current_concurrent(5), 3)
        self.assertEqual(RateLimiter(store=store).current_rpm(5), 3)

    def test_concurrent_probe_single_winner(self):
        cb = CircuitBreaker(store=self.store)
        for _ in range(5):
            cb.record_failure(2)
        self.store.set("cb:2:cooldown_until", "0")
        wins, barrier = [], threading.Barrier(10)

        def worker():
            barrier.wait()
            wins.append(CircuitBreaker(store=self.store).allow_request(2))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(wins.count(True), 1)

    def test_concurrent_failures_counted_atomically(self):
        cb = CircuitBreaker(store=self.store, error_threshold=1000)
        threads = [threading.Thread(target=cb.record_failure, args=(3,)) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(cb.consecutive_errors(3), 50)

    def test_redis_outage_degrades_to_memory(self):
        import redis
        client = MagicMock()
        for op in ("get", "set", "incr", "decr", "expire", "delete", "exists", "ttl",
                   "zadd", "zcard", "zremrangebyscore", "zrem"):
            getattr(client, op).side_effect = redis.ConnectionError("down")
        store = RedisStore(client=client)
        rl = RateLimiter(store=store)
        self.assertIsNotNone(rl.try_acquire(1, max_rpm=1, max_concurrent=1))
        self.assertIsNone(rl.try_acquire(1, max_rpm=1, max_concurrent=1))
        rl.release(1, "")
        cb = CircuitBreaker(store=store)
        self.assertTrue(cb.allow_request(1))
        with self.assertRaises(redis.ConnectionError):
            client.ping.side_effect = redis.ConnectionError("down")
            store.ping()  # 启动探活不降级，get_store 据此回落内存


class EdgeTTSAdapterTests(SimpleTestCase):
    def _fake_communicate(self, chunks, captured):
        class FakeCommunicate:
            def __init__(self, text, voice, **kwargs):
                captured.update(text=text, voice=voice, **kwargs)

            async def stream(self):
                for c in chunks:
                    if isinstance(c, Exception):
                        raise c
                    yield c

        return FakeCommunicate

    def _adapter(self, base_url=""):
        from apps.ai_gateway.adapters import build_adapter
        provider = MagicMock(adapter_type="edge_tts", provider_type="TTS", base_url=base_url)
        return build_adapter(provider)

    def test_synthesize_collects_audio_and_duration(self):
        captured = {}
        chunks = [
            {"type": "audio", "data": b"ab"},
            {"type": "SentenceBoundary", "offset": 1_000_000, "duration": 24_000_000, "text": "x"},
            {"type": "audio", "data": b"cd"},
        ]
        with patch("edge_tts.Communicate", self._fake_communicate(chunks, captured)), \
                patch.dict("os.environ", {"EDGE_TTS_PROXY": "http://proxy:7890"}):
            out = self._adapter("https://speech.platform.bing.com").synthesize("你&我", "", {"rate": "+10%"})
        self.assertEqual(out["audio_bytes"], b"abcd")
        self.assertEqual(out["duration_seconds"], 2.5)
        self.assertEqual(out["usage"]["audio_seconds"], 2.5)
        self.assertEqual(captured["voice"], "zh-CN-XiaoxiaoNeural")
        self.assertEqual(captured["rate"], "+10%")
        self.assertEqual(captured["proxy"], "http://proxy:7890")

    def test_duration_falls_back_to_estimate(self):
        chunks = [{"type": "audio", "data": b"x"}]
        with patch("edge_tts.Communicate", self._fake_communicate(chunks, {})):
            out = self._adapter().synthesize("一二三四五六七八", "zh-CN-YunxiNeural")
        self.assertEqual(out["duration_seconds"], 2.0)

    def test_errors_propagate_instead_of_hanging(self):
        chunks = [{"type": "audio", "data": b"x"}, ConnectionError("network down")]
        start = time.time()
        with patch("edge_tts.Communicate", self._fake_communicate(chunks, {})):
            with self.assertRaises(ConnectionError):
                self._adapter().synthesize("台词", "")
        self.assertLess(time.time() - start, 5)

    def test_empty_audio_and_text_rejected(self):
        with patch("edge_tts.Communicate", self._fake_communicate([], {})):
            with self.assertRaises(RuntimeError):
                self._adapter().synthesize("台词", "")
        with self.assertRaises(ValueError):
            self._adapter().synthesize("  ", "")
