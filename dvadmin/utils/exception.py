"""统一异常处理器（design D3 / platform/api-conventions）。

- 未预期异常 → HTTP 500 + {code:5000, msg:"服务器内部错误", data:{request_id}}，
  异常类型/信息/堆栈只进日志。
- 带 business_code 的自定义异常：把码挂到 response 上，交渲染器输出。
"""
import logging

from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger("dvadmin.exception")


class BusinessError(Exception):
    """带业务错误码的自定义异常。

    视图可 raise BusinessError(code=4090, msg="状态冲突", status=409)；
    本处理器把它转成 DRF Response。
    """

    def __init__(self, code, msg, status=400, data=None):
        self.code = code
        self.msg = msg
        self.status = status
        self.data = data
        super().__init__(msg)


def custom_exception_handler(exc, context):
    # 冻结剧的生成/发布派发被拒：业务码 4003（业务码命名空间，非信封 40x0 段）
    from apps.creation.pipeline.dispatcher import PlayFrozenError

    if isinstance(exc, PlayFrozenError):
        from rest_framework.response import Response

        response = Response(
            {"detail": f"短剧已被平台管理员冻结：{exc.play.frozen_reason}",
             "reason": "play_frozen"},
            status=403,
        )
        response.business_code = 4003
        return response

    # 自定义业务异常
    if isinstance(exc, BusinessError):
        from rest_framework.response import Response
        from rest_framework import status as http_status

        data = {"detail": exc.msg}
        if exc.data is not None:
            data["errors"] = exc.data
        response = Response(data, status=exc.status)
        response.business_code = exc.code
        return response

    response = drf_exception_handler(exc, context)

    if response is not None:
        return response

    # 未预期异常：转 500，细节只进日志
    request = context.get("request") if context else None
    request_id = getattr(request, "request_id", None)
    logger.exception("Unhandled exception (request_id=%s): %s", request_id, exc)
    from rest_framework.response import Response

    payload = {"detail": "服务器内部错误"}
    if request_id:
        payload["request_id"] = request_id
    response = Response(payload, status=500)
    response.business_code = 5000
    return response
