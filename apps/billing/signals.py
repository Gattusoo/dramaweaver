"""注册赠金信号（决策 16 / design D4）。

post_save on CustomUser create：事务内 grant 一笔 REGISTER_GRANT_POINTS
（默认 1000 分 = ¥10）；0 = 关闭。幂等：ref_kind=signup + ref_id=user_id
在 grant 内部按 (ref, type) 唯一判定——重复信号不重复发。
"""
import logging

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender="system.CustomUser")
def grant_signup_points(sender, instance, created, **kwargs):
    """注册事务内发放一次性赠金。"""
    if not created:
        return
    points = int(getattr(settings, "REGISTER_GRANT_POINTS", 0) or 0)
    if points <= 0:
        return  # 配置 0 即关闭
    from .services import grant
    try:
        entry = grant(instance, points, ref_kind="signup", ref_id=str(instance.pk),
                      note="注册赠金")
        if entry is not None:
            logger.info("注册赠金 user=%s +%s 分", instance.pk, points)
    except Exception as exc:  # noqa: BLE001 — 赠金失败不阻断注册
        logger.exception("注册赠金失败 user=%s: %s", instance.pk, exc)
