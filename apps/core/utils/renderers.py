"""统一响应渲染器（design D3 / platform/api-conventions）。

成功 → {code: 2000, msg: "ok", data}
分页 → {code: 2000, msg, page, limit, total, data}
错误 → {code, msg, data}（字段校验失败时 data.errors 按字段列出）
204 无响应体；文件下载与 SSE 不经 DRF 渲染器，天然不包装。
"""
from rest_framework.renderers import JSONRenderer

# 业务错误码
CODE_OK = 2000
CODE_BAD_REQUEST = 4000
CODE_UNAUTHORIZED = 4010
CODE_FORBIDDEN = 4030
CODE_NOT_FOUND = 4040
CODE_METHOD_NOT_ALLOWED = 4050
CODE_CONFLICT = 4090
CODE_THROTTLED = 4290
CODE_SERVER_ERROR = 5000

# HTTP 状态码 → 业务错误码
HTTP_TO_CODE = {
    400: CODE_BAD_REQUEST,
    401: CODE_UNAUTHORIZED,
    403: CODE_FORBIDDEN,
    404: CODE_NOT_FOUND,
    405: CODE_METHOD_NOT_ALLOWED,
    409: CODE_CONFLICT,
    429: CODE_THROTTLED,
}


def _http_status_to_code(status_code):
    if status_code in HTTP_TO_CODE:
        return HTTP_TO_CODE[status_code]
    if 500 <= status_code < 600:
        return CODE_SERVER_ERROR
    return CODE_BAD_REQUEST


class CustomJSONRenderer(JSONRenderer):
    """按统一结构包装 JSON 响应。"""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        response = renderer_context["response"] if renderer_context else None
        if response is None:
            return super().render(data, accepted_media_type, renderer_context)

        # 204 无内容；非 JSON 内容（文件下载等）原样返回
        if response.status_code == 204:
            return super().render(data, accepted_media_type, renderer_context)

        wrapped = self._wrap(response, data)
        return super().render(wrapped, accepted_media_type, renderer_context)

    def _wrap(self, response, data):
        status_code = response.status_code

        # 错误响应
        if status_code >= 400:
            code = getattr(response, "business_code", None) or _http_status_to_code(status_code)
            if isinstance(data, dict) and "detail" in data:
                msg = data["detail"]
                payload = {"code": code, "msg": msg, "data": None}
                # 附加业务字段原样透传到信封顶层（提案最新 status / 记忆 content+revision /
                # budget_exceeded 标记等）。信封三键与 detail/request_id 除外：冲突键丢弃。
                for k, v in data.items():
                    if k not in ("detail", "code", "msg", "data", "request_id"):
                        payload[k] = v
                # 5xx 异常：附 request_id（若异常处理器已写入 data）
                if isinstance(data, dict) and "request_id" in data:
                    payload["data"] = {"request_id": data["request_id"]}
                return payload
            # 无 detail：视为字段校验错误，原字典放进 data.errors
            msg = "参数校验失败"
            return {"code": CODE_BAD_REQUEST, "msg": msg, "data": {"errors": data}}

        # 分页结果：含四个键时提升
        if isinstance(data, dict) and {"page", "limit", "total", "results"} <= set(data.keys()):
            return {
                "code": CODE_OK,
                "msg": "ok",
                "page": data["page"],
                "limit": data["limit"],
                "total": data["total"],
                "data": data["results"],
            }

        # 普通成功
        return {"code": CODE_OK, "msg": "ok", "data": data}
