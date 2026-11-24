"""authorized_users → PlayMember 数据回填（幂等）。

- 每部剧：created_by → owner 行；authorized_users 每人 → active editor 行
  （决策 19：迁移不静默降权）
- get_or_create 按 (play, user) 唯一约束幂等，可重复运行
- 本迁移执行时 authorized_users 列仍存在（0013 才删列）
"""
from django.db import migrations
from django.utils import timezone


def backfill(apps, schema_editor):
    Play = apps.get_model("creation", "Play")
    PlayMember = apps.get_model("creation", "PlayMember")
    now = timezone.now()

    for play in Play.objects.all():
        # 属主 → owner 行（镜像 created_by，单一事实来源）
        owner_id = play.created_by_id
        if owner_id is not None:
            PlayMember.objects.get_or_create(
                play_id=play.pk, user_id=owner_id,
                defaults={"role": "owner", "status": "active", "accepted_at": now},
            )
        # 既有被授权用户 → active editor（不静默降权）
        for user_id in play.authorized_users.values_list("pk", flat=True):
            if user_id == owner_id:
                continue
            PlayMember.objects.get_or_create(
                play_id=play.pk, user_id=user_id,
                defaults={"role": "editor", "status": "active", "accepted_at": now},
            )


def unbackfill(apps, schema_editor):
    # 回滚：清空成员表（authorized_users 仍是权威来源，回到旧判定）
    PlayMember = apps.get_model("creation", "PlayMember")
    PlayMember.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("creation", "0011_play_membership"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
