"""短剧归属对象级权限（剧内成员表：view / edit / manage 三层）。

判定唯一来源：PlayMember（status=active）。全局角色（is_platform_admin）不赋予
业务接口访问权——平台管理员的跨剧查看走审核通道（views/admin_review.py）。
角色级权限（IsAdminOnly 等，读 is_platform_admin）在 apps.core.system.permissions。
"""
from django.db.models import Q
from rest_framework import permissions

from .models import MemberRole, MemberStatus, PlayMember


def _authed(user):
    return bool(user and user.is_authenticated)


def _resolve_play(obj):
    """Resolve the owning Play from any production-tree object."""
    from .models import Play

    if obj is None:
        return None
    if isinstance(obj, Play):
        return obj
    play = getattr(obj, "play", None)
    if play is not None:
        return play
    novel = getattr(obj, "novel", None)
    if novel is not None:
        return getattr(novel, "play", None)
    volume = getattr(obj, "volume", None)
    if volume is not None:
        return _resolve_play(volume)
    canvas = getattr(obj, "canvas", None)
    if canvas is not None:
        return getattr(canvas, "play", None)
    # 导演台族：元素挂 DirectorScene（scene__play）；绑定挂 shot（父链已有）
    director_scene = getattr(obj, "scene", None)
    if director_scene is not None and hasattr(director_scene, "play"):
        return getattr(director_scene, "play", None)
    shot = getattr(obj, "shot", None)
    if shot is not None:
        return _resolve_play(shot)
    # 剪辑族：片段挂 Track（track.composition.play）；工程/轨各自直挂 play
    composition = getattr(obj, "composition", None)
    if composition is not None:
        return getattr(composition, "play", None)
    track = getattr(obj, "track", None)
    if track is not None:
        return _resolve_play(track)
    parent = (
        getattr(obj, "episode", None)
        or getattr(obj, "scene", None)
        or getattr(obj, "downstream", None)
    )
    if parent is not None:
        return _resolve_play(parent)
    return None


def _active_member_role(user, play):
    """Active 成员行的角色；非成员返回 None。"""
    if play is None or not _authed(user):
        return None
    member = PlayMember.objects.filter(
        play_id=play.pk, user_id=user.pk, status=MemberStatus.ACTIVE
    ).only("role").first()
    return member.role if member else None


def is_play_member(user, play):
    """view 层：active 成员（任何角色）。"""
    return _active_member_role(user, play) is not None


def can_view_play(user, play):
    """view 层：active 成员即可。全局身份（含平台管理员）不参与判定。"""
    return is_play_member(user, play)


def can_write_play(user, play):
    """edit 层：owner / editor 成员。"""
    return _active_member_role(user, play) in (MemberRole.OWNER, MemberRole.EDITOR)


def can_manage_play(user, play):
    """manage 层：owner 成员（镜像 Play.created_by）。"""
    return _active_member_role(user, play) == MemberRole.OWNER


def triggered_by(user, play):
    """写操作触发者归因（成员变更任务 5.1 埋点，供计费变更的属主告警消费）。

    返回值：``owner``（属主本人）/ ``member``（其他成员）；无法判定时 ``unknown``。
    只做标识，不实现通知。
    """
    if user is None or play is None:
        return "unknown"
    user_pk = getattr(user, "pk", user)  # 同时接受 User 实例与用户 ID
    owner_id = getattr(play, "created_by_id", None)
    return "owner" if owner_id is not None and owner_id == user_pk else "member"


def get_accessible_play_ids(user):
    """IDs of Plays the user can view（自己是 active 成员的剧）。

    平台管理员不再天然全量可见——跨剧查看走审核通道。
    """
    from .models import Play

    if not user or not user.is_authenticated:
        return Play.objects.none().values_list("id", flat=True)
    return PlayMember.objects.filter(
        user_id=user.pk, status=MemberStatus.ACTIVE
    ).values_list("play_id", flat=True)


class IsPlayAuthorizedOrAdmin(permissions.BasePermission):
    """按所属 Play 判定的对象级权限（view / edit / manage 三层，全部查成员表）。"""

    message = "无权访问该短剧资源。"

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if not (request.user and request.user.is_authenticated):
            return False
        play = _resolve_play(obj)
        if play is None:
            # 非 Play 树对象（无归属）：仅平台管理员可操作（平台配置类资源）
            return bool(getattr(request.user, "is_platform_admin", False))
        if request.method in permissions.SAFE_METHODS:
            return can_view_play(request.user, play)
        # 只读语义的自定义 action（如 preview-redo 影响预检）：POST 但不写库，
        # 声明在 view.read_actions 中的按读权限放行
        if getattr(view, "action", None) in getattr(view, "read_actions", ()):
            return can_view_play(request.user, play)
        if getattr(view, "action", None) in getattr(view, "manage_actions", ()):
            return can_manage_play(request.user, play)
        return can_write_play(request.user, play)


class AssetLibraryPermission(permissions.BasePermission):
    """素材库：公共条目所有认证用户可读；私有条目仅 owner 可读可写。

    成员可在剧内使用属主私有素材的语义由生产路径（引用属主素材）表达，
    素材库条目本身不按剧授权。
    """

    message = "无权操作该素材库条目。"

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        user = request.user
        is_owner = obj.owner_id == user.pk
        if request.method in permissions.SAFE_METHODS:
            return obj.is_public or is_owner
        return is_owner
