"""request_id 中间件（design D3 / platform/api-conventions）。

为每个请求生成或回写 X-Request-ID，并在日志中带上该 ID，便于按 ID 排查。
未预期异常时，异常处理器会把 request_id 写进响应体。
"""
import logging
import uuid

logger = logging.getLogger("dvadmin.request")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware:
    """生成/回写请求 ID，并挂到 request 上供视图与异常处理器使用。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.request_id = request_id

        # 给当前日志记录绑定 request_id，便于按 ID 检索
        old_extra = getattr(logger, "extra", None)
        handler = logging.LogRecord
        # 用 LoggerAdapter 模式：在每条日志的 extra 中注入 request_id
        logger_adapter = logging.LoggerAdapter(logger, {"request_id": request_id})
        # 让模块级 logger 使用带 request_id 的 adapter（不影响其它 logger）
        request._request_id_logger = logger_adapter

        response = self.get_response(request)
        response[REQUEST_ID_HEADER] = request_id
        return response
