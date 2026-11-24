"""运行健康检查（工作台 Stage C2 任务 7.3，design D1）。

报告：部署模式、任务后端、worker 心跳（最近租约活跃度）、队列深度
（待领取/待恢复）、数据库与媒体目录可用性、FFmpeg/FFprobe 可用性。
脱敏约束：只返回布尔/计数/脱敏版本号，绝不回显路径与密钥。

worker 停止时：排队任务如实报告 queued 数，**不显示为生成中**（running
计数只统计租约未过期的执行——死租约视为中断，等待回收/恢复）。
"""
import shutil
import subprocess

from django.conf import settings
from django.db import connection
from django.utils import timezone

from ..models import ExecutionClaimStatus, LocalTaskExecution, TaskStatus

_LEASE_ACTIVE_WINDOW = 90  # 秒：最近 90s 内有租约活动即视为 worker 存活


def _ffmpeg_version(binary: str):
    """探测 FFmpeg/FFprobe：返回 (available, version|None)。超时/缺失安全。"""
    path = shutil.which(binary)
    if not path:
        return False, None
    try:
        out = subprocess.run(
            [path, "-version"], capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return False, None
        first = (out.stdout or "").splitlines()[0] if out.stdout else ""
        # 脱敏：只保留版本号段（如 "4.4.1"），不带路径/构建指纹
        version = ""
        for part in first.split():
            if part and part[0].isdigit() and "." in part:
                version = part
                break
        return True, version or "unknown"
    except (OSError, subprocess.SubprocessError):
        return False, None


def _worker_heartbeat() -> dict:
    """worker 存活判定：存在租约未过期（<90s 窗口）的 RUNNING 执行。"""
    now = timezone.now()
    window = timezone.timedelta(seconds=_LEASE_ACTIVE_WINDOW)
    recent = LocalTaskExecution.objects.filter(
        claim_status=ExecutionClaimStatus.RUNNING,
        lease_expires_at__gt=now,
    ).count()
    dead = LocalTaskExecution.objects.filter(
        claim_status=ExecutionClaimStatus.RUNNING,
        lease_expires_at__lte=now,
    ).count()
    return {"alive": recent > 0, "active_leases": recent, "stale_leases": dead}


def _queue_depth() -> dict:
    """队列深度：待领取（含到期重试）/待恢复确认。

    worker 停止时 queued 仍计数（如实排队），但 running 只算活跃租约——
    死租约不显示为生成中。
    """
    now = timezone.now()
    queued = LocalTaskExecution.objects.filter(
        claim_status=ExecutionClaimStatus.PENDING).count()
    ready_now = LocalTaskExecution.objects.filter(
        claim_status=ExecutionClaimStatus.PENDING,
        next_attempt_at__lte=now).count()
    recovery_required = LocalTaskExecution.objects.filter(
        claim_status=ExecutionClaimStatus.RECOVERY_REQUIRED).count()
    # RECOVERY_REQUIRED 的 TaskRecord 口径（shared 任务无执行行，按记录计）
    from ..models import TaskRecord
    recovery_records = TaskRecord.objects.filter(
        status=TaskStatus.RECOVERY_REQUIRED).count()
    return {
        "queued": queued,
        "ready_now": ready_now,
        "recovery_required": recovery_required,
        "recovery_records": recovery_records,
    }


def _data_dirs() -> dict:
    """数据目录可用性（布尔，不回显路径）。"""
    from ..workspace.storage import _root
    try:
        root = _root()
        writable = False
        try:
            probe = root / ".health_probe"
            probe.write_bytes(b"")
            probe.unlink()
            writable = True
        except OSError:
            writable = False
        return {"available": root.is_dir(), "writable": writable}
    except Exception:  # noqa: BLE001
        return {"available": False, "writable": False}


def health_report() -> dict:
    """聚合健康报告（脱敏：布尔/计数/版本号，无路径无密钥）。"""
    db_ok = True
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception:  # noqa: BLE001
        db_ok = False
    ffmpeg_ok, ffmpeg_ver = _ffmpeg_version("ffmpeg")
    ffprobe_ok, ffprobe_ver = _ffmpeg_version("ffprobe")
    return {
        "deployment_mode": "local" if getattr(settings, "IS_LOCAL_MODE", False) else "shared",
        "task_backend": (getattr(settings, "CREATION_TASK_BACKEND", "") or
                         ("local" if getattr(settings, "IS_LOCAL_MODE", False) else "celery")),
        "database": {"ok": db_ok},
        "worker": _worker_heartbeat(),
        "queue": _queue_depth(),
        "media": _data_dirs(),
        "ffmpeg": {"available": ffmpeg_ok, "version": ffmpeg_ver},
        "ffprobe": {"available": ffprobe_ok, "version": ffprobe_ver},
        "checked_at": timezone.now().isoformat(),
    }
