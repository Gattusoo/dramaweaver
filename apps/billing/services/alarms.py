"""属主大额消费告警埋点（proposal E / 任务 4.3）。

单任务 settle ≥ OWNER_ALERT_THRESHOLD_POINTS 时 emit 事件（含 triggered_by，
消费成员变更的埋点字段惯例）。本期只埋事件，站内信通道第二期。
"""
from __future__ import annotations

import logging

from django.conf import settings

from apps.ai_gateway.redis_client import get_store

logger = logging.getLogger(__name__)

# Redis 环形列表 key 与上限（对齐 failover events 惯例）
ALERT_EVENTS_KEY = "billing:owner_alerts"
ALERT_EVENTS_MAX = 200


def owner_alert_threshold() -> int:
    return int(getattr(settings, "OWNER_ALERT_THRESHOLD_POINTS", 0) or 0)


def maybe_alert_large_settle(owner, points: int, *, triggered_by=None,
                             task_ref: tuple[str, str] = ("", "")) -> bool:
    """单任务结算额超阈值时写告警事件。返回是否触发。

    triggered_by：触发者（剧内成员触发生成也记属主名下——透传给告警）。
    """
    threshold = owner_alert_threshold()
    if threshold <= 0 or points < threshold:
        return False
    event = {
        "type": "large_settle",
        "owner_id": owner.pk,
        "points": points,
        "triggered_by": getattr(triggered_by, "pk", None),
        "ref_kind": task_ref[0],
        "ref_id": task_ref[1],
    }
    import json

    store = get_store()
    raw = store.get(ALERT_EVENTS_KEY)
    events = json.loads(raw) if raw else []
    events.append(event)
    events = events[-ALERT_EVENTS_MAX:]
    store.set(ALERT_EVENTS_KEY, json.dumps(events))
    logger.info("大额消费告警 owner=%s points=%s triggered_by=%s",
                owner.pk, points, event["triggered_by"])
    return True


def read_alerts() -> list[dict]:
    import json

    store = get_store()
    raw = store.get(ALERT_EVENTS_KEY)
    return json.loads(raw) if raw else []
