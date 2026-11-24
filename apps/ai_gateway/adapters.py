"""4 类 AI 适配器：LLM / Image / TTS / Video。

每类适配器定义统一接口，各家 provider 通过 adapter_type 映射到具体实现。
底层 HTTP 调用使用 httpx；API 密钥从 AIProvider 记录读取。
"""
import asyncio
import base64
import json
import logging
import time
from abc import ABC, abstractmethod
from urllib.parse import urlparse, parse_qs
from xml.sax.saxutils import escape as xml_escape, quoteattr

import httpx

from .http import safe_post, safe_request, safe_stream

logger = logging.getLogger(__name__)


def get_api_key(provider):
    """Return the decrypted API key for a provider.

    当前按明文读取；后续可接入加密解密。api_key_encrypted 存储密文。
    """
    key = getattr(provider, "api_key_encrypted", "") or ""
    return key.strip()


# ─── LLM ──────────────────────────────────────────────────────────────────────

class LLMAdapter(ABC):
    """统一 LLM 接口。"""

    @abstractmethod
    def generate(self, prompt, system_prompt="", parameters=None):
        """返回 {text, usage: {input_tokens, output_tokens}, model}。"""

    def stream_generate(self, prompt, system_prompt="", parameters=None):
        """返回迭代器 [{text, usage}]。默认降级为一次性 generate。"""
        result = self.generate(prompt, system_prompt, parameters or {})
        yield {"text": result["text"], "usage": result.get("usage", {})}

    def chat(self, messages, system="", tools=None, parameters=None):
        """多轮 + 工具调用（content-block 格式，见 to_openai_messages 说明）。

        返回 {content_blocks, stop_reason: end_turn|tool_use|max_tokens, usage, model}。
        不支持工具调用的适配器保持默认实现（抛 NotImplementedError）。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持工具调用")


# ─── content-block <-> OpenAI tool_calls 转换（纯函数）────────────────────────
# 内部统一使用 Anthropic 风格：
#   messages: [{"role": "user"|"assistant", "content": str | [block]}]
#   block: {"type": "text", "text"} | {"type": "tool_use", "id", "name", "input"}
#          | {"type": "tool_result", "tool_use_id", "content", "is_error"?}
#   tools: [{"name", "description", "input_schema"}]

_STOP_REASONS = {"tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens"}


def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return list(content or [])


def _result_text(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def _image_url(block):
    """Anthropic 风格 image block -> OpenAI image_url 值（http(s) 或 data URI）。"""
    if block.get("url"):
        return block["url"]
    source = block.get("source") or {}
    data = source.get("data") or ""
    media_type = source.get("media_type") or "image/png"
    return f"data:{media_type};base64,{data}"


def to_openai_messages(messages, system=""):
    """content-block 消息 -> OpenAI chat messages。tool_result 拆成 role=tool 消息，
    紧跟在含 tool_calls 的 assistant 消息之后（OpenAI 要求的顺序）。

    image block（视觉分析，Phase 2.3）转成 OpenAI 多模态 content 数组。
    """
    out = [{"role": "system", "content": system}] if system else []
    for msg in messages:
        blocks = _blocks(msg.get("content"))
        if msg["role"] == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"],
                                   "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                     for b in blocks if b.get("type") == "tool_use"]
            item = {"role": "assistant", "content": text or None}
            if calls:
                item["tool_calls"] = calls
            out.append(item)
            continue
        for b in blocks:
            if b.get("type") == "tool_result":
                text = _result_text(b.get("content"))
                if b.get("is_error"):
                    text = f"[error] {text}"
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": text})
        images = [b for b in blocks if b.get("type") == "image"]
        if images:
            parts = []
            for b in blocks:
                if b.get("type") == "text" and b.get("text"):
                    parts.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "image":
                    parts.append({"type": "image_url", "image_url": {"url": _image_url(b)}})
            out.append({"role": "user", "content": parts})
            continue
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if text:
            out.append({"role": "user", "content": text})
    return out


def to_openai_tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object"}}}
            for t in tools or []]


def raise_on_error_envelope(data):
    """部分兼容实现返回 HTTP 200 但 body 含 error 字段——识别并抛业务错误。

    与 raise_for_status() 互补：那管非 2xx，这管 2xx + error envelope。"""
    err = data.get("error") if isinstance(data, dict) else None
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(f"Provider 返回业务错误: {msg}")


def from_openai_response(data, model):
    """OpenAI chat completion -> {content_blocks, stop_reason, usage, model}。"""
    raise_on_error_envelope(data)
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    blocks = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_raw_arguments": fn.get("arguments")}
        if not isinstance(args, dict):
            args = {"_raw_arguments": args}
        blocks.append({"type": "tool_use", "id": call.get("id", ""), "name": fn.get("name", ""), "input": args})
    reason = choice.get("finish_reason") or "stop"
    stop = _STOP_REASONS.get(reason, "end_turn")
    if any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"  # 部分兼容实现 finish_reason 仍为 stop
    usage = data.get("usage") or {}
    return {"content_blocks": blocks, "stop_reason": stop, "model": model,
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0)}}


class OpenAICompatLLMAdapter(LLMAdapter):
    """兼容 OpenAI 格式的 LLM（OpenAI / DeepSeek / Qwen / SenseNova 等）。"""

    def __init__(self, provider, timeout_seconds=60):
        self.provider = provider
        self.base_url = (getattr(provider, "base_url", "") or "").rstrip("/")
        self.api_key = get_api_key(provider)
        self.timeout = timeout_seconds

    def _endpoint(self):
        # base_url 形如 https://api.deepseek.com/v1 -> /chat/completions
        return f"{self.base_url}/chat/completions"

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def generate(self, prompt, system_prompt="", parameters=None):
        parameters = parameters or {}
        model = parameters.get("model", "")
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": model, "messages": messages}
        for key in ("temperature", "max_tokens", "top_p", "response_format"):
            if key in parameters and parameters[key] is not None:
                payload[key] = parameters[key]

        resp = safe_post(self.provider, self._endpoint(), headers=self._headers(), json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        raise_on_error_envelope(data)
        # 防御性解析与 from_openai_response/chat 对齐：choices 缺失、content 为 null
        # （如仅含 tool_calls）不抛 KeyError/IndexError/TypeError，降级空串
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        usage = data.get("usage", {})
        return {
            "text": text,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
            "model": model,
        }

    def chat(self, messages, system="", tools=None, parameters=None):
        parameters = parameters or {}
        model = parameters.get("model", "")
        payload = {"model": model, "messages": to_openai_messages(messages, system)}
        if tools:
            payload["tools"] = to_openai_tools(tools)
        for key in ("temperature", "max_tokens", "top_p"):
            if parameters.get(key) is not None:
                payload[key] = parameters[key]
        resp = safe_post(self.provider, self._endpoint(), headers=self._headers(), json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return from_openai_response(resp.json(), model)

    def stream_generate(self, prompt, system_prompt="", parameters=None):
        parameters = parameters or {}
        model = parameters.get("model", "")
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": model, "messages": messages, "stream": True}

        with safe_stream(self.provider, "POST", self._endpoint(),
                         headers=self._headers(), json=payload, timeout=self.timeout) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    delta = chunk
                    import json

                    obj = json.loads(delta)
                    content = obj["choices"][0]["delta"].get("content")
                    if content:
                        yield {"text": content, "usage": {}}
                except (ValueError, KeyError, IndexError):
                    continue


def _anthropic_content(content):
    """image block 归一：{url} → {source: {type: url}}；已有 source 原样保留。"""
    if isinstance(content, str):
        return content
    out = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "image" and "source" not in block:
            out.append({"type": "image", "source": {"type": "url", "url": block.get("url", "")}})
        else:
            out.append(block)
    return out


class ClaudeLLMAdapter(LLMAdapter):
    """Anthropic Claude Messages API 适配器。"""

    def __init__(self, provider, timeout_seconds=60):
        self.provider = provider
        self.base_url = (getattr(provider, "base_url", "") or "").rstrip("/") or "https://api.anthropic.com"
        self.api_key = get_api_key(provider)
        self.timeout = timeout_seconds

    def generate(self, prompt, system_prompt="", parameters=None):
        parameters = parameters or {}
        model = parameters.get("model", "claude-3-5-sonnet-latest")
        system = [{"type": "text", "text": system_prompt}] if system_prompt else []
        payload = {
            "model": model,
            "max_tokens": parameters.get("max_tokens", 4096),
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        resp = safe_post(self.provider, f"{self.base_url}/v1/messages", headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        return {
            "text": text,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            },
            "model": model,
        }

    def chat(self, messages, system="", tools=None, parameters=None):
        """content-block 透传 Messages API；image block 归一为 Anthropic source 结构。"""
        parameters = parameters or {}
        model = parameters.get("model", "claude-3-5-sonnet-latest")
        payload = {
            "model": model,
            "max_tokens": parameters.get("max_tokens", 4096),
            "messages": [{"role": m["role"], "content": _anthropic_content(m["content"])}
                         for m in messages],
        }
        if system:
            payload["system"] = [{"type": "text", "text": system}]
        if tools:
            payload["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                                 "input_schema": t.get("input_schema") or {"type": "object"}} for t in tools]
        if parameters.get("temperature") is not None:
            payload["temperature"] = parameters["temperature"]
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        resp = safe_post(self.provider, f"{self.base_url}/v1/messages", headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        blocks = [b for b in data.get("content", []) if b.get("type") in ("text", "tool_use")]
        stop = data.get("stop_reason") or "end_turn"
        usage = data.get("usage", {})
        return {
            "content_blocks": blocks,
            "stop_reason": stop if stop in ("tool_use", "max_tokens") else "end_turn",
            "usage": {"input_tokens": usage.get("input_tokens", 0),
                      "output_tokens": usage.get("output_tokens", 0)},
            "model": model,
        }


# ─── Image ────────────────────────────────────────────────────────────────────

class ImageAdapter(ABC):
    """统一图像生成接口。"""

    @abstractmethod
    def generate(self, prompt, reference_images=None, parameters=None):
        """返回 {image_bytes, url, usage: {image_count}}。"""

    def supports_reference(self):
        return False


class OpenAICompatImageAdapter(ImageAdapter):
    """兼容 OpenAI 图像格式的生成（OpenAI DALL·E / gpt-image-1、即梦 Seedream、火山 Ark、
    SiliconFlow 及任意自定义 OpenAI 兼容端点）。

    请求：POST {base_url}/images/generations，Bearer 鉴权，body {model, prompt, size, n}。
    响应：data[0] 优先取 url；若为 b64_json（gpt-image-1 只返回这个）则解码为 image_bytes。
    reference_images 不被消费（OpenAI 图像格式不原生支持参考图）。
    """

    def __init__(self, provider, timeout_seconds=120):
        self.provider = provider
        self.base_url = (getattr(provider, "base_url", "") or "").rstrip("/")
        self.api_key = get_api_key(provider)
        self.timeout = timeout_seconds

    def generate(self, prompt, reference_images=None, parameters=None):
        parameters = parameters or {}
        size = parameters.get("size", "1024x1792" if parameters.get("aspect_ratio") == "9:16" else "1024x1024")
        n = int(parameters.get("n", 1) or 1)
        payload = {"model": parameters.get("model", "dall-e-3"), "prompt": prompt, "size": size, "n": n}
        # 可选参数透传（存在时才加，保持 dall_e 行为不变）
        for key in ("quality", "style", "response_format"):
            if parameters.get(key) is not None:
                payload[key] = parameters[key]
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        resp = safe_post(self.provider, f"{self.base_url}/images/generations", headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        item = (data.get("data") or [{}])[0]
        url = item.get("url", "") or ""
        image_bytes = None
        if not url and item.get("b64_json"):
            image_bytes = base64.b64decode(item["b64_json"])
        return {"image_bytes": image_bytes, "url": url, "usage": {"image_count": n}}


class SDImageAdapter(ImageAdapter):
    """Stable Diffusion（ComfyUI 工作流）。支持参考图（IP-Adapter）。"""

    def __init__(self, provider, timeout_seconds=180):
        self.provider = provider
        self.base_url = (getattr(provider, "base_url", "") or "").rstrip("/")
        self.timeout = timeout_seconds

    def supports_reference(self):
        return True

    def generate(self, prompt, reference_images=None, parameters=None):
        parameters = parameters or {}
        # ComfyUI 工作流提交：上传参考图 -> 排队 -> 轮询 /history -> 取图
        raise NotImplementedError("SDImageAdapter 需配置 ComfyUI 工作流模板")


class FluxImageAdapter(ImageAdapter):
    """Black Forest Labs Flux（api.bfl.ai）。异步提交 + 轮询取图。

    提交：POST {base_url}/v1/{model}，header x-key，body {prompt, width, height, aspect_ratio, ...}
          -> {id}
    轮询：GET {base_url}/v1/get_result?id= 直到 status=="Ready" -> result.sample 为图片 url。
    reference_images 不被消费（BFL 文生图端点无 i2i）。
    """

    POLL_INTERVAL = 1.5  # 秒，可用环境变量 FLUX_POLL_INTERVAL 覆盖（供测试）
    _READY = "Ready"

    def __init__(self, provider, timeout_seconds=120):
        self.provider = provider
        self.base_url = (getattr(provider, "base_url", "") or "").rstrip("/") or "https://api.bfl.ai"
        self.api_key = get_api_key(provider)
        self.timeout = timeout_seconds

    def supports_reference(self):
        return False

    def _dims(self, aspect_ratio):
        # 常见比例 -> (width, height)；未识别则默认 1024x1024
        mapping = {
            "9:16": (768, 1344), "16:9": (1344, 768),
            "1:1": (1024, 1024), "3:4": (896, 1152), "4:3": (1152, 896),
            "9:21": (704, 1536), "21:9": (1536, 704),
        }
        return mapping.get(aspect_ratio, (1024, 1024))

    def _headers(self):
        return {"x-key": self.api_key, "Content-Type": "application/json"}

    def generate(self, prompt, reference_images=None, parameters=None):
        import os

        parameters = parameters or {}
        model = parameters.get("model", "flux-pro-1.1")
        aspect_ratio = parameters.get("aspect_ratio", "1:1")
        width, height = self._dims(aspect_ratio)
        body = {
            "prompt": prompt,
            "width": parameters.get("width", width),
            "height": parameters.get("height", height),
            "aspect_ratio": aspect_ratio,
            "output_format": parameters.get("output_format", "jpeg"),
        }
        if parameters.get("seed") is not None:
            body["seed"] = parameters["seed"]

        headers = self._headers()
        resp = safe_post(self.provider, f"{self.base_url}/v1/{model}",
                         headers=headers, json=body, timeout=self.timeout)
        resp.raise_for_status()
        task_id = resp.json().get("id")
        if not task_id:
            raise RuntimeError("Flux 提交未返回任务 id")

        interval = float(os.environ.get("FLUX_POLL_INTERVAL", self.POLL_INTERVAL))
        ceiling = max(60, self.timeout)
        deadline = time.time() + ceiling
        while True:
            if time.time() > deadline:
                raise TimeoutError(f"Flux 生成超时（>{ceiling}s）")
            poll = safe_request(self.provider, "GET", f"{self.base_url}/v1/get_result",
                                headers=headers, params={"id": task_id}, timeout=self.timeout)
            poll.raise_for_status()
            pdata = poll.json()
            status = pdata.get("status")
            if status == self._READY:
                url = (pdata.get("result") or {}).get("sample", "")
                return {"image_bytes": None, "url": url, "usage": {"image_count": 1}}
            if status == "Error":
                raise RuntimeError(f"Flux 生成失败: {pdata.get('details')}")
            time.sleep(interval)


# ─── TTS ──────────────────────────────────────────────────────────────────────

def _estimate_audio_duration(text, fallback_seconds=0):
    """估算语音时长（秒）：优先用调用方提供的值，否则按 ~4 字/秒估算。

    gateway._invoke 不传 duration_seconds，若不估算则 TTS 成本恒为 0。
    """
    if fallback_seconds:
        return float(fallback_seconds)
    if not text:
        return 0.0
    return max(1.0, len(text) / 4.0)


class TTSAdapter(ABC):
    """统一 TTS 接口。"""

    @abstractmethod
    def synthesize(self, text, voice_id, parameters=None):
        """返回 {audio_bytes, duration_seconds, usage: {audio_seconds}}。"""

    def list_voices(self):
        return []


class CosyVoiceTTSAdapter(TTSAdapter):
    """阿里 CosyVoice（或自建 CosyVoice）API。"""

    def __init__(self, provider, timeout_seconds=120):
        self.provider = provider
        self.base_url = (getattr(provider, "base_url", "") or "").rstrip("/")
        self.api_key = get_api_key(provider)
        self.timeout = timeout_seconds

    def synthesize(self, text, voice_id, parameters=None):
        parameters = parameters or {}
        payload = {"model": "cosyvoice-v2", "voice": voice_id, "text": text, "format": "mp3"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        resp = safe_post(self.provider, f"{self.base_url}/v1/tts", headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        audio = resp.content
        duration = _estimate_audio_duration(text, parameters.get("duration_seconds", 0))
        return {"audio_bytes": audio, "duration_seconds": duration, "usage": {"audio_seconds": duration}}


class AzureTTSAdapter(TTSAdapter):
    """Azure Speech TTS。"""

    def __init__(self, provider, timeout_seconds=120):
        self.provider = provider
        self.base_url = (getattr(provider, "base_url", "") or "").rstrip("/")
        self.api_key = get_api_key(provider)
        self.timeout = timeout_seconds

    def synthesize(self, text, voice_id, parameters=None):
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        }
        # 文本与属性值均做 XML 转义，防止 & < > ' " 破坏 SSML 或注入标签
        ssml = (
            "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>"
            f"<voice name={quoteattr(voice_id or '')}>{xml_escape(text or '')}</voice></speak>"
        )
        resp = safe_post(self.provider, f"{self.base_url}/cognitiveservices/v1", headers=headers, content=ssml.encode(), timeout=self.timeout)
        resp.raise_for_status()
        duration = _estimate_audio_duration(text, (parameters or {}).get("duration_seconds", 0))
        return {"audio_bytes": resp.content, "duration_seconds": duration, "usage": {"audio_seconds": duration}}


class EdgeTTSAdapter(TTSAdapter):
    """微软 Edge 在线朗读（edge-tts）：免 API Key，但需要访问外网。

    AIProvider 配置：adapter_type=edge_tts，base_url 为必填 URL 字段但不会被使用
    （可填 https://speech.platform.bing.com），api_key 可为空。
    需要代理时设置环境变量 EDGE_TTS_PROXY（如 http://127.0.0.1:7890）。
    voice_id 为空时用默认中文音色；parameters 支持 rate / volume / pitch（如 "+10%"、"+0Hz"）。
    """

    DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
    _TICKS_PER_SECOND = 10_000_000  # edge-tts offset/duration 单位为 100ns

    def __init__(self, provider, timeout_seconds=60):
        import os
        self.proxy = os.environ.get("EDGE_TTS_PROXY", "").strip() or None
        self.timeout = int(timeout_seconds)

    def synthesize(self, text, voice_id, parameters=None):
        parameters = parameters or {}
        if not (text or "").strip():
            raise ValueError("TTS 文本为空")
        audio, end_ticks = self._run_in_thread(
            text, voice_id or self.DEFAULT_VOICE, parameters
        )
        if not audio:
            raise RuntimeError("edge-tts 未返回音频数据")
        duration = end_ticks / self._TICKS_PER_SECOND if end_ticks else 0.0
        if duration <= 0:
            duration = _estimate_audio_duration(text, parameters.get("duration_seconds", 0))
        duration = round(duration, 3)
        return {"audio_bytes": audio, "duration_seconds": duration, "usage": {"audio_seconds": duration}}

    def _run_in_thread(self, text, voice, parameters):
        # 独立线程 + asyncio.run：调用方已有事件循环时也安全；
        # 异常经 future.result() 原样抛出（edge-tts 自带 stream_sync 出错会永久阻塞）
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, self._collect(text, voice, parameters))
            return future.result(timeout=self.timeout + 30)

    async def _collect(self, text, voice, parameters):
        import edge_tts

        communicate = edge_tts.Communicate(
            text, voice,
            rate=parameters.get("rate", "+0%"),
            volume=parameters.get("volume", "+0%"),
            pitch=parameters.get("pitch", "+0Hz"),
            proxy=self.proxy,
            receive_timeout=self.timeout,
        )
        chunks, end_ticks = [], 0
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                end_ticks = max(end_ticks, chunk["offset"] + chunk["duration"])
        return b"".join(chunks), end_ticks


class OpenAICompatTTSAdapter(TTSAdapter):
    """兼容 OpenAI 语音格式的 TTS（OpenAI tts-1、火山豆包经 Ark、SiliconFlow 及任意
    自定义 OpenAI 兼容端点）。

    请求：POST {base_url}/audio/speech，Bearer，body {model, input, voice, speed, response_format}。
    响应：原始音频字节（resp.content），非 JSON。
    """

    DEFAULT_VOICE = "alloy"

    def __init__(self, provider, timeout_seconds=120):
        self.provider = provider
        self.base_url = (getattr(provider, "base_url", "") or "").rstrip("/")
        self.api_key = get_api_key(provider)
        self.timeout = timeout_seconds

    def synthesize(self, text, voice_id, parameters=None):
        parameters = parameters or {}
        payload = {
            "model": parameters.get("model", "tts-1"),
            "input": text,
            "voice": voice_id or self.DEFAULT_VOICE,
            "response_format": parameters.get("response_format", "mp3"),
            "speed": parameters.get("speed", 1),
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        resp = safe_post(self.provider, f"{self.base_url}/audio/speech", headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        audio = resp.content
        duration = _estimate_audio_duration(text, parameters.get("duration_seconds", 0))
        return {"audio_bytes": audio, "duration_seconds": duration, "usage": {"audio_seconds": duration}}


class MiniMaxTTSAdapter(TTSAdapter):
    """MiniMax 海螺 TTS（原生 t2a_v2）。Bearer + GroupId。

    请求：POST {base_url}/v1/t2a_v2?GroupId={gid}，body 含 voice_setting / audio_setting。
    响应：JSON，data.audio 为 hex 编码音频（注意非 base64）；extra_info.audio_duration 为毫秒。
    GroupId 取自 base_url 的 query（如 https://api.minimax.chat?GroupId=xxx）或 parameters["group_id"]。
    """

    DEFAULT_VOICE = "male-qn-qingse"

    def __init__(self, provider, timeout_seconds=120):
        self.provider = provider
        self.base_url = (getattr(provider, "base_url", "") or "").rstrip("/")
        self.api_key = get_api_key(provider)
        self.timeout = timeout_seconds

    def _group_id(self, parameters):
        gid = (parameters or {}).get("group_id")
        if gid:
            return gid
        parsed = urlparse(self.base_url)
        qs = parse_qs(parsed.query)
        return (qs.get("GroupId") or [None])[0]

    def _url(self, path, group_id):
        """base_url 可能形如 https://api.minimax.chat?GroupId=xxx：取 scheme+host 拼路径，
        保留其原有 query（合并 GroupId）。"""
        parsed = urlparse(self.base_url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        return httpx.URL(f"{root}{path}", params={"GroupId": group_id})

    def synthesize(self, text, voice_id, parameters=None):
        parameters = parameters or {}
        group_id = self._group_id(parameters)
        if not group_id:
            raise ValueError("MiniMax TTS 需要 GroupId：在 base_url 加 ?GroupId=xxx 或传 parameters['group_id']")
        voice_setting = {
            "voice_id": voice_id or self.DEFAULT_VOICE,
            "speed": parameters.get("speed", 1.0),
            "vol": parameters.get("vol", 1.0),
            "pitch": parameters.get("pitch", 0),
        }
        payload = {
            "model": parameters.get("model", "speech-01-hd"),
            "text": text,
            "stream": False,
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": parameters.get("sample_rate", 32000),
                "bitrate": parameters.get("bitrate", 128000),
                "format": parameters.get("format", "mp3"),
                "channel": 1,
            },
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        resp = safe_post(self.provider, self._url("/v1/t2a_v2", group_id), headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax TTS 失败: {base_resp.get('status_msg')}")
        inner = data.get("data") or {}
        audio_hex = inner.get("audio", "")
        audio = bytes.fromhex(audio_hex) if audio_hex else b""
        extra = inner.get("extra_info") or {}
        duration_ms = extra.get("audio_duration")
        if duration_ms:
            duration = round(float(duration_ms) / 1000.0, 3)
        else:
            duration = _estimate_audio_duration(text, parameters.get("duration_seconds", 0))
        return {"audio_bytes": audio, "duration_seconds": duration, "usage": {"audio_seconds": duration}}


# ─── Video ────────────────────────────────────────────────────────────────────

# 视频生成普遍耗时 1–5 分钟，远超 AIProvider.timeout_seconds（默认 60，仅作单次 HTTP 超时）。
# 因此轮询总上限用固定值，不用 timeout_seconds。
VIDEO_POLL_INTERVAL = 5          # 秒
VIDEO_POLL_CEILING = 300         # 秒


class VideoAdapter(ABC):
    """统一图生视频接口。

    云端视频生成是异步的（提交任务 -> 轮询状态 -> 取结果），但业务侧
    （gateway._invoke / creation._video_segment）期望同步拿到 {url, duration_seconds}。
    因此 image_to_video 在内部完成 提交 -> 轮询 -> 取 url 并阻塞返回；
    check_status / retrieve_result 是它组合的原语。

    wait=False 时只提交并立即返回（url 置空 + task_id 标记），供管理端连通性测试
    廉价验证提交、不挂起。
    """

    @abstractmethod
    def image_to_video(self, image_url, parameters=None, *, wait=True):
        """提交并（默认）阻塞轮询到完成，返回 {url, duration_seconds, task_id?}。"""

    @abstractmethod
    def _submit(self, image_url, parameters):
        """提交任务，返回 task_id。"""

    @abstractmethod
    def _poll_once(self, task_id):
        """查询一次状态，返回 {status, url?, duration_seconds?, progress?}。
        status ∈ pending/processing/completed/failed。"""


def _poll_video(adapter, task_id, wait, *, interval=VIDEO_POLL_INTERVAL, ceiling=VIDEO_POLL_CEILING):
    """共享轮询循环。wait=False 时立即返回未完成标记；否则轮询到 completed/failed/超时。"""
    if not wait:
        return {"url": "", "duration_seconds": 0, "task_id": task_id, "pending": True}
    import os

    interval = float(os.environ.get("VIDEO_POLL_INTERVAL", interval))
    deadline = time.time() + ceiling
    while True:
        if time.time() > deadline:
            raise TimeoutError(f"视频生成超时（>{ceiling}s），task_id={task_id}")
        state = adapter._poll_once(task_id)
        status = state.get("status")
        if status == "completed":
            return {"url": state.get("url", ""), "duration_seconds": state.get("duration_seconds", 0),
                    "task_id": task_id}
        if status == "failed":
            raise RuntimeError(f"视频生成失败，task_id={task_id}: {state.get('error', 'unknown')}")
        time.sleep(interval)


class KlingVideoAdapter(VideoAdapter):
    """可灵 Kling（api.klingai.com）。JWT 鉴权。

    api_key_encrypted 约定存 `access_key:secret_key`（冒号分隔），用 HS256 签 JWT，
    Authorization: Bearer <jwt>。缺失冒号时抛 ValueError。
    """

    _STATUS_MAP = {"succeed": "completed", "failed": "failed"}

    def __init__(self, provider, timeout_seconds=60):
        self.provider = provider
        self.base_url = (getattr(provider, "base_url", "") or "").rstrip("/") or "https://api.klingai.com"
        self.api_key = get_api_key(provider)
        self.timeout = timeout_seconds

    def _token(self):
        if ":" not in self.api_key:
            raise ValueError("可灵 api_key 必须为 'access_key:secret_key'（冒号分隔）")
        import jwt

        ak, sk = self.api_key.split(":", 1)
        now = int(time.time())
        headers = {"alg": "HS256", "typ": "JWT"}
        payload = {"iss": ak, "exp": now + 1800, "nbf": now - 5}
        return jwt.encode(payload, sk, algorithm="HS256", headers=headers)

    def _headers(self):
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    def image_to_video(self, image_url, parameters=None, *, wait=True):
        parameters = parameters or {}
        body = {
            "model_name": parameters.get("model", "kling-v1"),
            "image": image_url,
            "prompt": parameters.get("prompt", ""),
            "duration": str(parameters.get("duration", "5")),
            "aspect_ratio": parameters.get("aspect_ratio", "9:16"),
            "mode": parameters.get("mode", "std"),
        }
        if parameters.get("cfg_scale") is not None:
            body["cfg_scale"] = parameters["cfg_scale"]
        resp = safe_post(self.provider, f"{self.base_url}/v1/videos/image2video",
                         headers=self._headers(), json=body, timeout=self.timeout)
        resp.raise_for_status()
        task_id = (resp.json().get("data") or {}).get("task_id")
        if not task_id:
            raise RuntimeError("可灵提交未返回 task_id")
        return _poll_video(self, task_id, wait)

    def _submit(self, image_url, parameters):
        raise NotImplementedError  # 由 image_to_video 内联实现

    def _poll_once(self, task_id):
        resp = safe_request(self.provider, "GET",
                            f"{self.base_url}/v1/videos/image2video/{task_id}",
                            headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        raw = data.get("task_status", "")
        status = self._STATUS_MAP.get(raw, "processing")
        videos = ((data.get("task_result") or {}).get("videos") or [{}])
        first = videos[0] if videos else {}
        return {"status": status, "url": first.get("url", ""),
                "duration_seconds": first.get("duration", 0), "error": raw}


class HailuoVideoAdapter(VideoAdapter):
    """海螺（MiniMax）视频。Bearer + GroupId。

    GroupId 取自 base_url 的 query（如 https://api.minimax.chat?GroupId=xxx）或
    parameters["group_id"]。
    """

    _STATUS_MAP = {"Success": "completed", "Fail": "failed"}

    def __init__(self, provider, timeout_seconds=60):
        self.provider = provider
        self.base_url = (getattr(provider, "base_url", "") or "").rstrip("/") or "https://api.minimax.chat"
        self.api_key = get_api_key(provider)
        self.timeout = timeout_seconds

    def _group_id(self, parameters):
        gid = (parameters or {}).get("group_id")
        if gid:
            return gid
        parsed = urlparse(self.base_url)
        qs = parse_qs(parsed.query)
        return (qs.get("GroupId") or [None])[0]

    def _url(self, path, group_id, **extra_params):
        """base_url 可能形如 https://api.minimax.chat?GroupId=xxx：取 scheme+host 拼路径，
        保留其原有 query（合并 GroupId 及额外参数）。"""
        parsed = urlparse(self.base_url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        params = {"GroupId": group_id, **extra_params}
        return httpx.URL(f"{root}{path}", params=params)

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def image_to_video(self, image_url, parameters=None, *, wait=True):
        parameters = parameters or {}
        group_id = self._group_id(parameters)
        if not group_id:
            raise ValueError("海螺视频需要 GroupId：在 base_url 加 ?GroupId=xxx 或传 parameters['group_id']")
        body = {
            "model": parameters.get("model", "T2V-01"),
            "prompt": parameters.get("prompt", ""),
            "first_frame_image": image_url,
            "duration": parameters.get("duration", 6),
            "prompt_optimizer": True,
        }
        resp = safe_post(self.provider, self._url("/v1/video_generation", group_id),
                         headers=self._headers(), json=body, timeout=self.timeout)
        resp.raise_for_status()
        task_id = resp.json().get("task_id")
        if not task_id:
            raise RuntimeError("海螺提交未返回 task_id")
        return _poll_video(self, task_id, wait)

    def _submit(self, image_url, parameters):
        raise NotImplementedError  # 由 image_to_video 内联实现

    def _poll_once(self, task_id):
        group_id = self._group_id({})
        resp = safe_request(self.provider, "GET",
                            self._url("/v1/query/video_generation", group_id, task_id=task_id),
                            headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("status", "")
        status = self._STATUS_MAP.get(raw, "processing")
        file_info = data.get("file") or {}
        return {"status": status, "url": file_info.get("download_url", ""),
                "duration_seconds": file_info.get("duration", 0), "error": raw}


# ─── Factory ──────────────────────────────────────────────────────────────────

# 热门供应商 + 自定义第三方接入。
# LLM 供应商绝大多数是 OpenAI 兼容格式（base_url + api_key + model 即可接入），
# 因此统一映射到 OpenAICompatLLMAdapter，无需重复实现。
_ADAPTER_MAP = {
    # ── LLM：OpenAI 兼容（只需 base_url + api_key + model）──
    "openai": OpenAICompatLLMAdapter,        # OpenAI
    "deepseek": OpenAICompatLLMAdapter,      # 深度求索
    "qwen": OpenAICompatLLMAdapter,          # 阿里通义千问
    "sensenova": OpenAICompatLLMAdapter,     # 商汤日日新
    "zhipu": OpenAICompatLLMAdapter,         # 智谱 GLM
    "kimi": OpenAICompatLLMAdapter,          # 月之暗面 Kimi
    "moonshot": OpenAICompatLLMAdapter,      # Moonshot（同 Kimi）
    "wenxin": OpenAICompatLLMAdapter,        # 百度文心一言
    "doubao": OpenAICompatLLMAdapter,        # 字节火山豆包
    "hunyuan": OpenAICompatLLMAdapter,       # 腾讯混元
    "minimax": OpenAICompatLLMAdapter,       # MiniMax
    "gemini": OpenAICompatLLMAdapter,        # Google Gemini（OpenAI 兼容端点）
    "ollama": OpenAICompatLLMAdapter,        # 本地 Ollama（http://localhost:11434/v1）
    "siliconflow": OpenAICompatLLMAdapter,   # 硅基流动
    "openrouter": OpenAICompatLLMAdapter,    # OpenRouter 聚合
    "groq": OpenAICompatLLMAdapter,          # Groq
    # 自定义第三方：任意 OpenAI 兼容端点
    "custom_openai": OpenAICompatLLMAdapter,
    # ── LLM：专有协议 ──
    "claude": ClaudeLLMAdapter,              # Anthropic
    # ── IMAGE ── OpenAI 兼容（DALL·E / Seedream / 火山 Ark / 自定义）
    "dall_e": OpenAICompatImageAdapter,      # OpenAI DALL-E 3 / gpt-image-1
    "seedream": OpenAICompatImageAdapter,    # 即梦 Seedream（火山 Ark，OpenAI 兼容）
    "jimeng": OpenAICompatImageAdapter,      # 即梦（同 Seedream）
    "custom_openai_image": OpenAICompatImageAdapter,  # 自定义 OpenAI 兼容图像端点
    "sd_comfyui": SDImageAdapter,            # Stable Diffusion (ComfyUI，占位)
    "flux": FluxImageAdapter,                # BFL Flux（异步提交+轮询）
    # ── TTS ──
    "cosyvoice": CosyVoiceTTSAdapter,        # 阿里 CosyVoice
    "azure_tts": AzureTTSAdapter,            # Azure Speech
    "edge_tts": EdgeTTSAdapter,              # 微软 Edge 在线朗读（免 Key）
    "openai_tts": OpenAICompatTTSAdapter,    # OpenAI tts-1（OpenAI 兼容 /audio/speech）
    "volcengine_doubao": OpenAICompatTTSAdapter,  # 火山豆包（经 Ark /audio/speech）
    "minimax_tts": MiniMaxTTSAdapter,        # MiniMax 海螺（原生 t2a_v2）
    "custom_openai_tts": OpenAICompatTTSAdapter,  # 自定义 OpenAI 兼容语音端点
    # ── VIDEO ──
    "kling": KlingVideoAdapter,              # 可灵 Kling（JWT，异步轮询）
    "hailuo": HailuoVideoAdapter,            # 海螺 MiniMax（异步轮询）
}

# 热门供应商元数据（adapter_type -> 说明），供文档/管理界面展示。
SUPPORTED_ADAPTERS = {
    "openai": "OpenAI",
    "deepseek": "DeepSeek 深度求索",
    "qwen": "阿里通义千问",
    "sensenova": "商汤日日新",
    "zhipu": "智谱 GLM",
    "kimi": "月之暗面 Kimi",
    "moonshot": "Moonshot",
    "wenxin": "百度文心一言",
    "doubao": "字节火山豆包",
    "hunyuan": "腾讯混元",
    "minimax": "MiniMax",
    "gemini": "Google Gemini",
    "ollama": "本地 Ollama",
    "siliconflow": "硅基流动",
    "openrouter": "OpenRouter 聚合",
    "groq": "Groq",
    "custom_openai": "自定义第三方（OpenAI 兼容 LLM）",
    "claude": "Anthropic Claude",
    "dall_e": "OpenAI DALL-E 3 / gpt-image-1",
    "seedream": "即梦 Seedream（火山 Ark）",
    "jimeng": "即梦（火山 Ark）",
    "custom_openai_image": "自定义第三方（OpenAI 兼容图像）",
    "sd_comfyui": "Stable Diffusion (ComfyUI)",
    "flux": "BFL Flux",
    "cosyvoice": "阿里 CosyVoice",
    "azure_tts": "Azure Speech",
    "edge_tts": "微软 Edge TTS（免 Key，需外网）",
    "openai_tts": "OpenAI TTS",
    "volcengine_doubao": "火山豆包 TTS",
    "minimax_tts": "MiniMax 海螺 TTS",
    "custom_openai_tts": "自定义第三方（OpenAI 兼容语音）",
    "kling": "可灵 Kling",
    "hailuo": "海螺 MiniMax 视频",
}


def build_adapter(provider):
    """根据 provider.adapter_type + provider_type 构造对应适配器。

    自定义第三方接入：adapter_type 设为 `custom_openai`（或任意 OpenAI 兼容
    供应商标识），仅需在 AIProvider 配置 base_url + api_key，并在 AIModel
    配置 model_name，即可接入任何 OpenAI 兼容端点。
    """
    adapter_type = (getattr(provider, "adapter_type", "") or "").strip().lower()
    if not adapter_type or adapter_type == "custom":
        # 回退：LLM/IMAGE 默认走 OpenAI 兼容（第三方最通用），其余给类型默认
        defaults = {
            "LLM": "custom_openai",
            "IMAGE": "dall_e",
            "TTS": "cosyvoice",
            "VIDEO": "kling",
        }
        adapter_type = defaults.get(provider.provider_type, "custom_openai")
    cls = _ADAPTER_MAP.get(adapter_type)
    if cls is None:
        raise ValueError(
            f"未知适配器类型: {adapter_type}；可选: {', '.join(SUPPORTED_ADAPTERS)}"
        )
    # 管理员显式配置的超时优先；仍为字段默认值时保留各适配器自己的默认（视频 180s 等）
    timeout = getattr(provider, "timeout_seconds", None)
    field_default = provider._meta.get_field("timeout_seconds").default if hasattr(provider, "_meta") else None
    if timeout and timeout != field_default:
        return cls(provider, timeout_seconds=timeout)
    return cls(provider)
