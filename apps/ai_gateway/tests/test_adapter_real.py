"""真实适配器单测（HTTP 层用 httpx.MockTransport 拦截，不发真实网络请求）。

覆盖：OpenAICompatImageAdapter / OpenAICompatTTSAdapter / MiniMaxTTSAdapter /
FluxImageAdapter / KlingVideoAdapter / HailuoVideoAdapter。
校验请求形状（路径、鉴权、关键字段）与返回契约（与业务侧消费一致）。
"""
import base64
import json
import time
from unittest.mock import patch

from django.test import SimpleTestCase
import httpx

from apps.ai_gateway.adapters import (
    OpenAICompatImageAdapter,
    OpenAICompatTTSAdapter,
    MiniMaxTTSAdapter,
    FluxImageAdapter,
    KlingVideoAdapter,
    HailuoVideoAdapter,
)


class _Provider:
    """轻量 provider 替身：适配器只读 base_url / api_key_encrypted。"""

    def __init__(self, base_url, api_key="test-key", adapter_type=""):
        self.base_url = base_url
        self.api_key_encrypted = api_key
        self.adapter_type = adapter_type
        self.provider_type = ""


def _json_response(payload, status_code=200):
    return httpx.Response(status_code, json=payload,
                          request=httpx.Request("GET", "https://example.com"))


class _ClientPatcher:
    """把 ai_gateway.http 模块里的 httpx.Client / httpx.stream 替换为带
    MockTransport 的实现，并绕过 SSRF 校验（check_url/check_peer）。

    出站防护本身有独立测试覆盖（见 test_url_safety_egress.py）；本 patcher
    只用于让适配器逻辑测试可以命中 MockTransport，不被 SSRF 校验阻塞。

    用法：
        with _ClientPatcher(handler):
            adapter.generate(...)
    handler(request) -> httpx.Response
    """

    def __init__(self, handler):
        self.transport = httpx.MockTransport(handler)
        self._real_client = httpx.Client
        self._real_stream = httpx.stream

    def _client_factory(self, *args, **kwargs):
        kwargs.pop("transport", None)
        # follow_redirects 由 safe_request 控制为 False；MockTransport 下不强求
        kwargs.pop("follow_redirects", None)
        return self._real_client(transport=self.transport, **kwargs)

    def _stream_factory(self, *args, **kwargs):
        # httpx.stream(method, url, **kw) 是 context manager，内部建 Client+send(stream=True)
        # 用 MockTransport 的 client 包装实现等价行为
        kwargs.pop("transport", None)
        kwargs.pop("follow_redirects", None)
        method, url, *rest = args
        client = self._real_client(transport=self.transport)

        class _StreamCtx:
            def __enter__(_self):
                req = client.build_request(method, url, **kwargs)
                _self.resp = client.send(req, stream=True)
                return _self.resp

            def __exit__(_self, *exc):
                _self.resp.close()
                client.close()
                return False

        return _StreamCtx()

    def __enter__(self):
        self._p_client = patch("apps.ai_gateway.http.httpx.Client", self._client_factory)
        self._p_stream = patch("apps.ai_gateway.http.httpx.stream", self._stream_factory)
        self._p_check_url = patch("apps.ai_gateway.http.check_url", lambda url: "")
        self._p_check_peer = patch("apps.ai_gateway.http.check_peer", lambda host, peer: None)
        self._p_client.start()
        self._p_stream.start()
        self._p_check_url.start()
        self._p_check_peer.start()
        return self

    def __exit__(self, *exc):
        self._p_client.stop()
        self._p_stream.stop()
        self._p_check_url.stop()
        self._p_check_peer.stop()
        return False


class OpenAICompatImageAdapterTests(SimpleTestCase):
    def _adapter(self, base_url="https://api.openai.com/v1", **kw):
        return OpenAICompatImageAdapter(_Provider(base_url, **kw))

    def test_dall_e_url_parity(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = json.loads(request.content)
            return _json_response({"data": [{"url": "http://img/1.png"}]})

        with _ClientPatcher(handler):
            result = self._adapter().generate("a cat", [], {})
        self.assertEqual(seen["path"], "/v1/images/generations")
        self.assertEqual(seen["auth"], "Bearer test-key")
        self.assertEqual(seen["body"]["model"], "dall-e-3")
        self.assertEqual(seen["body"]["n"], 1)
        self.assertEqual(seen["body"]["size"], "1024x1024")
        self.assertEqual(result, {"image_bytes": None, "url": "http://img/1.png",
                                  "usage": {"image_count": 1}})

    def test_portrait_size(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return _json_response({"data": [{"url": "u"}]})

        with _ClientPatcher(handler):
            self._adapter().generate("x", [], {"aspect_ratio": "9:16"})
        self.assertEqual(seen["body"]["size"], "1024x1792")

    def test_b64_response_decoded(self):
        raw = b"fake-image-bytes"

        def handler(request):
            return _json_response({"data": [{"b64_json": base64.b64encode(raw).decode()}]})

        with _ClientPatcher(handler):
            result = self._adapter().generate("x", [], {"model": "gpt-image-1"})
        self.assertEqual(result["image_bytes"], raw)
        self.assertEqual(result["url"], "")

    def test_custom_model_and_size_override(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return _json_response({"data": [{"url": "u"}]})

        with _ClientPatcher(handler):
            self._adapter(base_url="https://ark.volces.com/api/v3").generate(
                "x", [], {"model": "doubao-seedream-3-0-t2i", "size": "2048x2048"})
        self.assertEqual(seen["body"]["model"], "doubao-seedream-3-0-t2i")
        self.assertEqual(seen["body"]["size"], "2048x2048")


class OpenAICompatTTSAdapterTests(SimpleTestCase):
    def test_synthesize_binary_body(self):
        seen = {}
        audio = b"\x00\x01\x02mp3data"

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, content=audio,
                                  request=httpx.Request("POST", "https://example.com"))

        with _ClientPatcher(handler):
            result = OpenAICompatTTSAdapter(_Provider("https://api.openai.com/v1")).synthesize(
                "你好", "alloy", {})
        self.assertEqual(seen["path"], "/v1/audio/speech")
        self.assertEqual(seen["body"]["input"], "你好")
        self.assertEqual(seen["body"]["voice"], "alloy")
        self.assertEqual(result["audio_bytes"], audio)
        self.assertGreater(result["duration_seconds"], 0)
        self.assertEqual(result["usage"]["audio_seconds"], result["duration_seconds"])

    def test_default_voice_when_empty(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, content=b"x",
                                  request=httpx.Request("POST", "https://example.com"))

        with _ClientPatcher(handler):
            OpenAICompatTTSAdapter(_Provider("https://api.openai.com/v1")).synthesize("hi", "", {})
        self.assertEqual(seen["body"]["voice"], "alloy")


class MiniMaxTTSAdapterTests(SimpleTestCase):
    def test_hex_audio_decoded(self):
        audio = b"minimax-audio"
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            seen["body"] = json.loads(request.content)
            return _json_response({
                "base_resp": {"status_code": 0, "status_msg": "success"},
                "data": {
                    "audio": audio.hex(),
                    "extra_info": {"audio_duration": 2500},
                },
            })

        provider = _Provider("https://api.minimax.chat?GroupId=12345")
        with _ClientPatcher(handler):
            result = MiniMaxTTSAdapter(provider).synthesize("你好", "male-qn-qingse", {})
        self.assertIn("GroupId=12345", seen["url"])
        self.assertIn("/v1/t2a_v2", seen["url"])
        self.assertEqual(seen["auth"], "Bearer test-key")
        self.assertEqual(seen["body"]["voice_setting"]["voice_id"], "male-qn-qingse")
        self.assertEqual(result["audio_bytes"], audio)
        self.assertEqual(result["duration_seconds"], 2.5)

    def test_group_id_from_params(self):
        def handler(request):
            return _json_response({"base_resp": {"status_code": 0},
                                   "data": {"audio": b"x".hex(), "extra_info": {}}})

        provider = _Provider("https://api.minimax.chat")
        with _ClientPatcher(handler):
            result = MiniMaxTTSAdapter(provider).synthesize("hi", "v", {"group_id": "999"})
        self.assertEqual(result["audio_bytes"], b"x")

    def test_missing_group_id_raises(self):
        provider = _Provider("https://api.minimax.chat")
        with self.assertRaises(ValueError):
            MiniMaxTTSAdapter(provider).synthesize("hi", "v", {})

    def test_api_error_raises(self):
        def handler(request):
            return _json_response({"base_resp": {"status_code": 1004, "status_msg": "bad"}})

        provider = _Provider("https://api.minimax.chat?GroupId=1")
        with _ClientPatcher(handler):
            with self.assertRaises(RuntimeError):
                MiniMaxTTSAdapter(provider).synthesize("hi", "v", {})


class FluxImageAdapterTests(SimpleTestCase):
    def test_submit_and_poll(self):
        calls = {"n": 0}
        seen = {}

        def handler(request):
            seen["xkey"] = request.headers.get("x-key")
            if request.url.path.endswith("/flux-pro-1.1"):
                seen["submit_body"] = json.loads(request.content)
                return _json_response({"id": "task-123"})
            # poll
            calls["n"] += 1
            if calls["n"] == 1:
                return _json_response({"status": "Processing", "progress": 0.4})
            return _json_response({"status": "Ready",
                                   "result": {"sample": "http://img/flux.png"}})

        provider = _Provider("https://api.bfl.ai", adapter_type="flux")
        with patch("time.sleep", lambda s: None):
            with _ClientPatcher(handler):
                result = FluxImageAdapter(provider).generate("a cat", [], {"aspect_ratio": "9:16"})
        self.assertEqual(seen["xkey"], "test-key")
        self.assertEqual(seen["submit_body"]["prompt"], "a cat")
        self.assertEqual(seen["submit_body"]["aspect_ratio"], "9:16")
        self.assertEqual(result["url"], "http://img/flux.png")
        self.assertEqual(result["usage"]["image_count"], 1)

    def test_error_status_raises(self):
        def handler(request):
            if request.url.path.endswith("/flux-pro-1.1"):
                return _json_response({"id": "task-1"})
            return _json_response({"status": "Error", "details": "bad prompt"})

        provider = _Provider("https://api.bfl.ai")
        with patch("time.sleep", lambda s: None):
            with _ClientPatcher(handler):
                with self.assertRaises(RuntimeError):
                    FluxImageAdapter(provider).generate("x", [], {})

    def test_supports_reference_false(self):
        self.assertFalse(FluxImageAdapter(_Provider("https://api.bfl.ai")).supports_reference())


class KlingVideoAdapterTests(SimpleTestCase):
    def _adapter(self, api_key="ak:sk"):
        return KlingVideoAdapter(_Provider("https://api.klingai.com", api_key=api_key))

    def test_submit_poll_success(self):
        calls = {"n": 0}
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            if request.method == "POST":
                seen["path"] = request.url.path
                seen["body"] = json.loads(request.content)
                return _json_response({"data": {"task_id": "kl-1", "task_status": "submitted"}})
            calls["n"] += 1
            if calls["n"] == 1:
                return _json_response({"data": {"task_status": "processing"}})
            return _json_response({"data": {"task_status": "succeed",
                                            "task_result": {"videos": [
                                                {"url": "http://v/kling.mp4", "duration": 5}]}}})

        with patch("time.sleep", lambda s: None):
            with _ClientPatcher(handler):
                result = self._adapter().image_to_video("http://img/1.png", {"duration": 5})
        self.assertTrue(seen["auth"].startswith("Bearer "))
        self.assertEqual(seen["path"], "/v1/videos/image2video")
        self.assertEqual(seen["body"]["image"], "http://img/1.png")
        self.assertEqual(seen["body"]["duration"], "5")
        self.assertEqual(result["url"], "http://v/kling.mp4")
        self.assertEqual(result["duration_seconds"], 5)

    def test_bad_api_key_format(self):
        with self.assertRaises(ValueError):
            self._adapter(api_key="justakey").image_to_video("http://img/1.png", {}, wait=False)

    def test_wait_false_only_submits(self):
        posts = {"n": 0}

        def handler(request):
            if request.method == "POST":
                posts["n"] += 1
                return _json_response({"data": {"task_id": "kl-1"}})
            raise AssertionError("wait=False 不应轮询")

        with _ClientPatcher(handler):
            result = self._adapter().image_to_video("http://img/1.png", {}, wait=False)
        self.assertEqual(posts["n"], 1)
        self.assertTrue(result.get("pending"))
        self.assertEqual(result["task_id"], "kl-1")

    def test_poll_timeout(self):
        def handler(request):
            if request.method == "POST":
                return _json_response({"data": {"task_id": "kl-1"}})
            return _json_response({"data": {"task_status": "processing"}})

        clock = {"t": 1000.0}

        def fake_time():
            clock["t"] += 100
            return clock["t"]

        with patch("time.sleep", lambda s: None), patch("time.time", fake_time):
            with _ClientPatcher(handler):
                with self.assertRaises(TimeoutError):
                    self._adapter().image_to_video("http://img/1.png", {})

    def test_failed_status_raises(self):
        def handler(request):
            if request.method == "POST":
                return _json_response({"data": {"task_id": "kl-1"}})
            return _json_response({"data": {"task_status": "failed"}})

        with patch("time.sleep", lambda s: None):
            with _ClientPatcher(handler):
                with self.assertRaises(RuntimeError):
                    self._adapter().image_to_video("http://img/1.png", {})


class HailuoVideoAdapterTests(SimpleTestCase):
    def _adapter(self):
        return HailuoVideoAdapter(_Provider("https://api.minimax.chat?GroupId=777",
                                            api_key="mk"))

    def test_submit_poll_success(self):
        calls = {"n": 0}
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            if request.method == "POST":
                seen["path"] = request.url.path
                seen["body"] = json.loads(request.content)
                self_in_url = "GroupId=777" in str(request.url)
                seen["group"] = self_in_url
                return _json_response({"task_id": "mm-1", "base_resp": {"status_code": 0}})
            calls["n"] += 1
            if calls["n"] == 1:
                return _json_response({"status": "Processing"})
            return _json_response({"status": "Success",
                                   "file": {"download_url": "http://v/mm.mp4", "duration": 6}})

        with patch("time.sleep", lambda s: None):
            with _ClientPatcher(handler):
                result = self._adapter().image_to_video("http://img/1.png", {"duration": 6})
        self.assertEqual(seen["auth"], "Bearer mk")
        self.assertEqual(seen["path"], "/v1/video_generation")
        self.assertEqual(seen["body"]["first_frame_image"], "http://img/1.png")
        self.assertTrue(seen["group"])
        self.assertEqual(result["url"], "http://v/mm.mp4")
        self.assertEqual(result["duration_seconds"], 6)

    def test_missing_group_id_raises(self):
        adapter = HailuoVideoAdapter(_Provider("https://api.minimax.chat", api_key="mk"))
        with self.assertRaises(ValueError):
            adapter.image_to_video("http://img/1.png", {})

    def test_failed_status_raises(self):
        def handler(request):
            if request.method == "POST":
                return _json_response({"task_id": "mm-1"})
            return _json_response({"status": "Fail"})

        with patch("time.sleep", lambda s: None):
            with _ClientPatcher(handler):
                with self.assertRaises(RuntimeError):
                    self._adapter().image_to_video("http://img/1.png", {})


class AdapterMapTests(SimpleTestCase):
    """确认新 adapter_type 都能构造出对应适配器（不发起网络）。"""

    def test_new_types_build(self):
        from apps.ai_gateway.adapters import build_adapter

        cases = {
            "custom_openai_image": OpenAICompatImageAdapter,
            "seedream": OpenAICompatImageAdapter,
            "openai_tts": OpenAICompatTTSAdapter,
            "minimax_tts": MiniMaxTTSAdapter,
            "kling": KlingVideoAdapter,
            "hailuo": HailuoVideoAdapter,
        }
        for adapter_type, cls in cases.items():
            provider = _Provider("https://example.com", adapter_type=adapter_type)
            provider.provider_type = {"seedream": "IMAGE", "custom_openai_image": "IMAGE",
                                      "openai_tts": "TTS", "minimax_tts": "TTS",
                                      "kling": "VIDEO", "hailuo": "VIDEO"}[adapter_type]
            self.assertIsInstance(build_adapter(provider), cls, adapter_type)
