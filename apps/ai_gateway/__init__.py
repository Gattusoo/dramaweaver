"""AI Gateway：所有 AI 调用的统一入口。

包含适配器层（adapters）、路由降级层（router）、熔断器（circuit_breaker）、
限流（rate_limiter）、成本统计（cost_tracker）及主入口（gateway）。
"""
