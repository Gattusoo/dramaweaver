# -*- coding: utf-8 -*-
"""billing 的 Celery 任务包装（供 shared 部署的 beat 调度）。"""
from celery import shared_task


@shared_task(name="shortplay.billing.expire_stale_holds")
def expire_stale_holds_task():
    """回收过期预扣（幂等，可重复调度）。

    shared 部署建议 beat 每 15 分钟一次；local 模式无 beat 进程不调度
    （settle/refund 幂等，不影响任务终态对账——见 docs/DEPLOY.md 计费节）。
    """
    from apps.billing.services.wallet import expire_stale_holds
    return expire_stale_holds()
