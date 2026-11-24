"""创作助手 API（assistant/session + assistant/proposal，D7）。

- 会话：仅创建者可读写，管理员可读；创建与发消息需要对短剧有写权限（会产生模型费用）
- 提案：短剧成员可见；apply / discard 需 can_write_play
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import Play, TaskRecord, TaskStatus, TaskType
from apps.core.system.permissions import is_platform_admin
from ..permissions import can_write_play, get_accessible_play_ids
from . import events
from .models import AssistantMessage, AssistantSession, MessageRole, ProposalStatus
from .proposals import ProposalError, apply_proposal, discard_proposal
from .serializers import (
    AgentFeedbackSerializer, AssistantExperimentSerializer, AssistantMessageSerializer,
    AssistantProposalSerializer, AssistantSessionSerializer, AssistantSkillLibrarySerializer,
    CharacterVersionSerializer, InspirationSerializer, ReviewFeedbackSerializer,
    ScriptVersionSerializer,
)

STALE_RUNNING = timedelta(minutes=15)   # RUNNING 超过该时长视为 worker 已崩溃
MESSAGE_RPM = 10
MAX_MESSAGE_CHARS = 8000
_TERMINAL = (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED)


def active_task(session):
    """会话当前运行中的轮次；终态或疑似崩溃的记录视为空闲。"""
    task = session.running_task
    if task is None or task.status in _TERMINAL:
        return None
    started = task.started_at or task.created_at
    if started and timezone.now() - started > STALE_RUNNING:
        return None
    return task


def _is_admin(user):
    """平台管理员判定（GLOBAL 技能库等平台级入口）。"""
    return is_platform_admin(user) or user.is_superuser


def _strict_bool(value, default=False):
    """布尔入参严格解析：只认 bool / "true"/"false"（大小写不敏感）。

    bool("false") 是 True——表单编码的字符串布尔经 bool() 会意外翻转为真。
    不合类型时返回 default（并应由调用方或序列化器拒绝）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return default


def _play_or_404(user, pk, select_related_agent=True):
    """数字 pk + 成员权限过滤 → Play，否则 404。各 *ViewSet 共用。"""
    if not str(pk).isdigit():
        raise NotFound("短剧不存在或无权访问")
    qs = Play.objects.filter(pk=int(pk), pk__in=get_accessible_play_ids(user))
    if select_related_agent:
        qs = qs.select_related("agent_config")
    play = qs.first()
    if play is None:
        raise NotFound("短剧不存在或无权访问")
    return play


class AssistantSessionViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                              viewsets.GenericViewSet):
    serializer_class = AssistantSessionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # 会话是业务数据：平台管理员不经业务接口跨剧读取（审核走专用通道）
        qs = AssistantSession.objects.select_related("running_task")
        qs = qs.filter(user=self.request.user)
        play = self.request.query_params.get("play")
        if play:
            qs = qs.filter(play_id=play) if str(play).isdigit() else qs.none()
        return qs

    def _own(self):
        session = self.get_object()
        if session.user_id != self.request.user.pk:
            raise PermissionDenied("只能操作自己的会话")
        return session

    def perform_create(self, serializer):
        play = Play.objects.filter(pk=serializer.validated_data.pop("play_id"),
                                   pk__in=get_accessible_play_ids(self.request.user)).first()
        if play is None:
            raise ValidationError({"play_id": "短剧不存在或无权访问"})
        if not can_write_play(self.request.user, play):
            raise PermissionDenied("对该短剧只有只读权限，不能使用创作助手")
        serializer.save(user=self.request.user, play=play, agent_config=play.agent_config,
                        title=serializer.validated_data.get("title") or play.title)

    def retrieve(self, request, *args, **kwargs):
        session = self.get_object()
        data = self.get_serializer(session).data
        data["messages"] = AssistantMessageSerializer(session.messages.order_by("seq"), many=True).data
        data["last_cursor"] = events.last_cursor(session.pk)
        return Response(data)

    @action(detail=True, methods=["post"])
    def messages(self, request, pk=None):
        """POST /api/assistant/sessions/{id}/messages/ {text} -> 202 {task_record_id, cursor, seq}。"""
        text = request.data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError({"text": "消息不能为空"})
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValidationError({"text": f"消息过长（上限 {MAX_MESSAGE_CHARS} 字）"})
        session = self._own()
        from .runner import _save, over_budget
        with transaction.atomic():
            session = AssistantSession.objects.select_for_update().select_related(
                "running_task", "play", "agent_config").get(pk=session.pk)
            if not can_write_play(request.user, session.play):
                raise PermissionDenied("对该短剧只有只读权限，不能使用创作助手")
            running = active_task(session)
            if running is not None:
                # Phase 3.2（registry D5）：轮次运行中 → 有界指令队列（不触发新轮次）。
                # 开关关闭时保持既有"上一轮尚未结束"冲突。
                return self._maybe_queue_message(request, session, running, text)
            if over_budget(session):
                # reason 而非 code：code 与统一信封的业务码键冲突，渲染器会丢弃（见 renderers.py）
                return Response({"detail": "会话费用已达上限，请联系管理员调高预算", "reason": "budget_exceeded"},
                                status=status.HTTP_409_CONFLICT)
            if not _throttle(request.user):
                return Response({"detail": f"发送过于频繁（每分钟最多 {MESSAGE_RPM} 条）"},
                                status=status.HTTP_429_TOO_MANY_REQUESTS)
            record = TaskRecord.objects.create(play=session.play, task_type=TaskType.ASSISTANT_TURN,
                                               status=TaskStatus.PENDING, input_summary={"session_id": session.pk})
            gate_resp = _assistant_billing_gate(record)
            if gate_resp is not None:
                return gate_resp
            message = _save(session, MessageRole.USER, [{"type": "text", "text": text}], record)
            AssistantSession.objects.filter(pk=session.pk).update(running_task=record, updated_at=timezone.now())
            cursor = events.last_cursor(session.pk)
            # 提交后再派发：避免 worker 先于事务提交读取 TaskRecord
            transaction.on_commit(lambda: _send(record))
        return Response({"task_record_id": record.pk, "cursor": cursor, "seq": message.seq,
                         "message": AssistantMessageSerializer(message).data},
                        status=status.HTTP_202_ACCEPTED)

    def _maybe_queue_message(self, request, session, running, text):
        """Phase 3.2（realtime D5）：轮次运行中提交指令。

        开关关闭 → 既有"上一轮尚未结束"冲突（行为与建设前一致）；
        开启 → 队列未满落库 USER 消息带 queued_on（不派发新轮次）；
        队列满 → 409 queue_full。限流同消息发送。
        """
        from django.conf import settings
        if not getattr(settings, "ENABLE_REALTIME_EVENTS", False):
            return Response({"detail": "上一轮对话尚未结束"}, status=status.HTTP_409_CONFLICT)
        from .runner import _save
        pending = session.messages.filter(queued_on=running).count()
        max_queue = int(getattr(settings, "TURN_QUEUE_MAX", 3))
        if pending >= max_queue:
            return Response({"detail": f"运行中指令队列已满（每轮最多 {max_queue} 条）",
                             "reason": "queue_full"}, status=status.HTTP_409_CONFLICT)
        if not _throttle(request.user):
            return Response({"detail": f"发送过于频繁（每分钟最多 {MESSAGE_RPM} 条）"},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        message = _save(session, MessageRole.USER, [{"type": "text", "text": text}], running)
        AssistantMessage.objects.filter(pk=message.pk).update(queued_on=running)
        message.refresh_from_db()
        events.emit(session.pk, "queued", {"seq": message.seq, "text": text})
        return Response({"queued": True, "task_record_id": running.pk, "seq": message.seq,
                         "message": AssistantMessageSerializer(message).data},
                        status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"])
    def collaborate(self, request, pk=None):
        """POST /api/assistant/sessions/{id}/collaborate/ {goal, competitive?, styles?}。

        Phase 2.1：触发一次多 Agent 协作轮次（异步），复用 TaskRecord / 事件流 / 预算。
        """
        from django.conf import settings
        if not getattr(settings, "ENABLE_MULTI_AGENT", False):
            raise ValidationError({"detail": "多Agent协作未启用"})
        goal = request.data.get("goal") or request.data.get("text")
        if not isinstance(goal, str) or not goal.strip():
            raise ValidationError({"goal": "协作目标不能为空"})
        if len(goal) > MAX_MESSAGE_CHARS:
            raise ValidationError({"goal": f"协作目标过长（上限 {MAX_MESSAGE_CHARS} 字）"})
        competitive_raw = request.data.get("competitive", False)
        if not isinstance(competitive_raw, (bool, str)):
            raise ValidationError({"competitive": "应为布尔值"})
        if isinstance(competitive_raw, str) and competitive_raw.strip().lower() not in ("true", "false"):
            raise ValidationError({"competitive": "应为布尔值（true/false）"})
        competitive = _strict_bool(competitive_raw)
        if competitive and not getattr(settings, "ENABLE_AGENT_COMPETITION", False):
            # Phase 0 max 审查修复（5.3）：竞争模式会为同一目标并行多次模型调用，
            # 必须受自身开关约束——否则关闭竞争仍产生成倍费用。
            raise ValidationError({"competitive": "竞争模式未启用（ENABLE_AGENT_COMPETITION=false）"})
        styles = request.data.get("styles") or []
        if not isinstance(styles, list) or any(not isinstance(s, str) for s in styles):
            raise ValidationError({"styles": "styles 须为字符串数组"})
        styles = [s.strip() for s in styles if s.strip()][:3]

        session = self._own()
        from .runner import _save, over_budget
        with transaction.atomic():
            session = AssistantSession.objects.select_for_update().select_related(
                "running_task", "play", "agent_config").get(pk=session.pk)
            if not can_write_play(request.user, session.play):
                raise PermissionDenied("对该短剧只有只读权限，不能使用创作助手")
            if active_task(session):
                return Response({"detail": "上一轮对话尚未结束"}, status=status.HTTP_409_CONFLICT)
            if over_budget(session):
                return Response({"detail": "会话费用已达上限，请联系管理员调高预算", "reason": "budget_exceeded"},
                                status=status.HTTP_409_CONFLICT)
            if not _throttle(request.user):
                return Response({"detail": f"发送过于频繁（每分钟最多 {MESSAGE_RPM} 条）"},
                                status=status.HTTP_429_TOO_MANY_REQUESTS)
            record = TaskRecord.objects.create(
                play=session.play, task_type=TaskType.ASSISTANT_TURN, status=TaskStatus.PENDING,
                input_summary={"session_id": session.pk, "mode": "collaborate",
                               "competitive": competitive, "styles": styles})
            gate_resp = _assistant_billing_gate(record)
            if gate_resp is not None:
                return gate_resp
            message = _save(session, MessageRole.USER, [{"type": "text", "text": goal}], record)
            AssistantSession.objects.filter(pk=session.pk).update(running_task=record,
                                                                  updated_at=timezone.now())
            cursor = events.last_cursor(session.pk)
            transaction.on_commit(lambda: _send(record))
        return Response({"task_record_id": record.pk, "cursor": cursor, "seq": message.seq,
                         "message": AssistantMessageSerializer(message).data},
                        status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        """GET /api/assistant/sessions/{id}/events/?cursor=&wait= 长轮询（最多 25 秒）。"""
        session = self.get_object()
        try:
            wait = int(request.query_params.get("wait", 0))
        except ValueError:
            wait = 0
        items, cursor = events.read(session.pk, request.query_params.get("cursor") or "0-0", wait)
        return Response({"events": items, "cursor": cursor})

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        session = self._own()
        task = active_task(session)
        if task is None:
            return Response({"detail": "没有运行中的轮次"}, status=status.HTTP_409_CONFLICT)
        updated = TaskRecord.objects.filter(pk=task.pk).exclude(status__in=_TERMINAL).update(
            status=TaskStatus.CANCELLED)
        if updated and task.status == TaskStatus.PENDING:
            # 尚未开始：worker 可能永远不会执行，这里直接收尾，前端据 done 停止订阅
            TaskRecord.objects.filter(pk=task.pk).update(completed_at=timezone.now())
            AssistantSession.objects.filter(pk=session.pk, running_task=task).update(running_task=None)
            events.emit(session.pk, "cancelled")
            events.emit(session.pk, "done", {"outcome": "cancelled"})
        return Response({"task_record_id": task.pk, "status": TaskStatus.CANCELLED})

    @action(detail=True, methods=["get"], url_path="tree")
    def tree(self, request, pk=None):
        """GET /api/assistant/sessions/{id}/tree/ -> 会话树可视化（JSON结构）。

        权威来源按 ENABLE_CONTEXT_COMPACTION 区分（phase1 max 审查修复 1.1）：
        - 开关开启：树随轮次落盘（runner._tree_sync），JSONL 是权威来源，
          直接返回已落盘树、不做数据库重建——重建会覆盖 compaction/
          branch_summary 条目并重置 active_entry_id，压缩静默失效。
        - 开关关闭：树不随轮次同步，DB 重建（确定性 ID msg-{pk}）落盘一次
          使 active_entry_id 跨请求稳定、navigate 可用；快照落后于数据库
          （新消息未入树）时重建落盘——否则新消息不可导航。"""
        from django.conf import settings as dj_settings
        from .session_tree import SessionStorage, SessionTreeManager
        session = self._own()
        file_path = SessionStorage.get_session_file(str(session.play_id), str(session.pk))
        compaction_on = getattr(dj_settings, "ENABLE_CONTEXT_COMPACTION", False)
        if compaction_on:
            tree = SessionTreeManager.get_or_create(session)
            if not file_path.exists():
                SessionStorage.save(tree)
        else:
            tree = SessionTreeManager.get_or_create(session)
            # 快照过期判定（phase1 修复 1.2）：重建路径对每条 DB 消息生成一个
            # 条目（user_message/assistant_message，content.message_id 映射），
            # 故以「可映射条目数」与 DB 消息数比较——旧实现数了 tool_result
            # 却漏了 assistant_message，任何含 assistant 回复的会话恒判过期。
            mapped = sum(1 for e in tree.entries
                         if isinstance(e.content, dict) and "message_id" in e.content)
            stale = file_path.exists() and mapped < session.messages.count()
            if stale or not file_path.exists():
                # 首次（重建）落盘或过期重建：后续请求复用同一文件（未过期时），
                # entry_id/active_entry_id 不漂移（确定性 ID 保证）
                tree = SessionTreeManager._rebuild_from_db(session)
                SessionStorage.save(tree)
        structure = tree.get_tree_structure()
        return Response({
            "session_id": session.pk,
            "tree": structure,
            "active_entry_id": tree.active_entry_id,
            "total_entries": len(tree.entries),
        })

    @action(detail=True, methods=["post"], url_path="tree/navigate")
    def tree_navigate(self, request, pk=None):
        """POST /api/assistant/sessions/{id}/tree/navigate/ -> 导航到某个条目。

        phase1 修复 7.1：写权限校验与 active_task 守卫（与 messages/compact 一致）——
        导航是改树操作（写 active_entry_id/branch_summary），只读成员 403、
        轮次运行中 409。phase1 修复 7.2：摘要 LLM 调用计入会话预算（task_record
        归因 → CostRecord → session_cost）并受 _throttle 限流约束。"""
        session = self._own()
        if not can_write_play(request.user, session.play):
            raise PermissionDenied("对该短剧只有只读权限，不能导航会话分支")
        if active_task(session):
            return Response({"detail": "上一轮对话尚未结束"}, status=status.HTTP_409_CONFLICT)
        if not _throttle(request.user):
            return Response({"detail": f"操作过于频繁（每分钟最多 {MESSAGE_RPM} 次）"},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        entry_id = request.data.get("entry_id")
        if not entry_id:
            raise ValidationError({"entry_id": "必填"})
        from .session_tree import SessionTreeManager
        tree = SessionTreeManager.get_or_create(session)
        # phase1 修复 7.2：摘要 LLM 费用归因到本会话的 ASSISTANT_TURN 记录
        # （无运行轮次时用轻量归因记录，使 CostRecord 可统计入 session_cost）
        summary_record = _summary_attribution_record(session)
        # Phase 1.4: 导航前生成被放弃分支摘要（须在navigate_to改活动条目前调用）
        from .session_tree import BranchSummarizer
        branch_summary = None
        try:
            branch_summary = BranchSummarizer.on_navigate(session, tree, entry_id,
                                                           task_record=summary_record)
        except Exception:  # noqa: BLE001 — 摘要失败不阻断导航
            import logging
            logging.getLogger(__name__).exception("分支摘要生成失败 session=%s", session.pk)
        if not tree.navigate_to(entry_id):
            raise ValidationError({"entry_id": "条目不存在"})
        from .session_tree import SessionStorage
        SessionStorage.save(tree)
        return Response({
            "active_entry_id": tree.active_entry_id,
            "branch_summary": branch_summary,
            "branch": [{"id": e.id, "type": e.type, "content": e.content}
                      for e in tree.get_active_branch()],
        })

    @action(detail=True, methods=["post"], url_path="tree/fork")
    def tree_fork(self, request, pk=None):
        """POST /api/assistant/sessions/{id}/tree/fork/ -> 从某个条目创建新分支。"""
        from .session_tree import SessionTreeManager, SessionStorage
        session = self._own()
        entry_id = request.data.get("entry_id")
        if not entry_id:
            raise ValidationError({"entry_id": "必填"})
        tree = SessionTreeManager.get_or_create(session)
        entry = tree.get_entry(entry_id)
        if not entry:
            raise ValidationError({"entry_id": "条目不存在"})
        # 返回该条目的完整路径（供前端展示或创建新会话）
        path = tree.fork(entry_id)
        return Response({
            "entry_id": entry_id,
            "path": [{"id": e.id, "type": e.type, "content": e.content} for e in path],
        })

    @action(detail=True, methods=["post"], url_path="compact")
    def compact(self, request, pk=None):
        """POST /api/assistant/sessions/{id}/compact/ {instructions?: str} -> 手动压缩上下文。

        对齐Pi Agent /compact：LLM生成结构化摘要替换早期历史，原始条目保留在会话树。
        instructions 可选，用于聚焦摘要重点（如"保留角色设定相关决策"）。
        """
        session = self._own()
        instructions = request.data.get("instructions") or ""
        if not isinstance(instructions, str):
            raise ValidationError({"instructions": "应为字符串"})
        if len(instructions) > 1000:
            raise ValidationError({"instructions": "过长（上限1000字）"})
        if active_task(session):
            return Response({"detail": "上一轮对话尚未结束，暂不能压缩"},
                            status=status.HTTP_409_CONFLICT)
        try:
            from .compaction import compact_session
            result = compact_session(session, instructions)
        except Exception as exc:  # noqa: BLE001 — 压缩失败不影响会话本身
            return Response({"detail": f"压缩失败: {exc}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(result.to_dict())

    @action(detail=True, methods=["post"])
    def feedback(self, request, pk=None):
        """POST /api/assistant/sessions/{id}/feedback/ {turn?, rating, comment?}

        Phase 3.2.8：对协作轮择优决策的反馈（1-5 星 + 评论）；turn 缺省取
        最近协作轮。同轮重复提交覆盖。低分反馈注入本会话后续轮次。
        """
        from ..models import TaskType
        from .models import AgentFeedback
        session = self._own()
        rating = request.data.get("rating")
        if not isinstance(rating, int) or isinstance(rating, bool) or not (1 <= rating <= 5):
            raise ValidationError({"rating": "必填且为 1-5 的整数"})
        comment = request.data.get("comment") or ""
        if len(comment) > 500:
            raise ValidationError({"comment": "评论超过 500 字上限"})
        turn = request.data.get("turn")
        if turn is None:
            record = (session.running_task and session.running_task.task_type == TaskType.ASSISTANT_TURN
                      and session.running_task) or None
            if record is None:
                record = (TaskRecord.objects.filter(
                    input_summary__mode="collaborate",
                    input_summary__session_id=session.pk)
                    .order_by("-created_at").first())
            if record is None:
                return Response({"detail": "无可反馈的协作轮次"},
                                status=status.HTTP_404_NOT_FOUND)
        else:
            if not isinstance(turn, int):
                raise ValidationError({"turn": "应为整数"})
            record = TaskRecord.objects.filter(pk=turn).first()
            if record is None:
                return Response({"detail": "轮次不存在"},
                                status=status.HTTP_404_NOT_FOUND)
        obj, _ = AgentFeedback.objects.update_or_create(
            session=session, user=request.user, turn=record,
            defaults={"rating": rating, "comment": comment})
        return Response({"rating": obj.rating, "comment": obj.comment,
                         "turn": record.pk})

    @action(detail=True, methods=["get"], url_path="plan")
    def plan(self, request, pk=None):
        """GET /api/assistant/sessions/{id}/plan/ -> 当前活跃计划及进度。"""
        from .models import PlanStatus
        from .planning import PlanningEngine
        session = self._own()
        plan = PlanningEngine.active_plan(session)
        if plan is None:
            return Response({"plan": None, "detail": "当前无活跃计划"})
        return Response({"plan": {
            "id": plan.pk, "goal": plan.goal, "status": plan.status,
            "steps": plan.steps, "progress": plan.get_progress(),
            "revisions": plan.revisions.count(),
            "created_at": plan.created_at, "updated_at": plan.updated_at,
        }})

    @action(detail=True, methods=["post"], url_path="plan/cancel")
    def plan_cancel(self, request, pk=None):
        """POST /api/assistant/sessions/{id}/plan/cancel/ -> 放弃当前活跃计划。"""
        from .models import PlanStatus
        from .planning import PlanningEngine
        session = self._own()
        plan = PlanningEngine.active_plan(session)
        if plan is None:
            return Response({"detail": "当前无活跃计划"}, status=status.HTTP_404_NOT_FOUND)
        PlanningEngine.finish_plan(plan, PlanStatus.ABANDONED)
        events.emit(session.pk, "plan_abandoned", {"plan_id": plan.pk, "goal": plan.goal})
        return Response({"plan_id": plan.pk, "status": PlanStatus.ABANDONED,
                         "completed_steps": [s for s in plan.steps
                                             if s.get("status") == "COMPLETED"]})

    @action(detail=True, methods=["get"], url_path=r"turns/(?P<turn_id>\d+)/trace")
    def turn_trace(self, request, pk=None, turn_id=None):
        """GET /api/assistant/sessions/{id}/turns/{record_id}/trace/ -> 该轮 trace（span 瀑布图）。

        仅会话属主（或管理员）可读；轮次须属于本会话，trace 取自 TaskRecord.output_summary。
        轮次未结束或无 trace 时返回 status=pending，不 404（轮次可能仍在跑）。
        """
        session = self._own()
        record = TaskRecord.objects.filter(pk=int(turn_id),
                                           input_summary__session_id=session.pk).first()
        if record is None:
            return Response({"detail": "轮次不存在"}, status=status.HTTP_404_NOT_FOUND)
        trace = (record.output_summary or {}).get("trace")
        if trace is None:
            return Response({"task_record_id": record.pk, "status": record.status,
                             "detail": "轮次尚未产出 trace"})
        return Response({"task_record_id": record.pk, "status": record.status, "trace": trace})


def _send(record):
    from ..pipeline.dispatcher import TASK_NAME_MAP, send_task
    result = send_task(TASK_NAME_MAP[TaskType.ASSISTANT_TURN], args=[record.pk])
    if result is not None:
        TaskRecord.objects.filter(pk=record.pk).update(celery_task_id=getattr(result, "id", "") or "")


def _assistant_billing_gate(record):
    """助手轮次的计费派发闸（billing 变更接线）：拒绝时返回 Response，通过返回 None。

    hold ref 按轮次独立（("task_record", record.pk)）——每轮预扣/对账独立，
    不与 pipeline 侧共享语义。余额不足 → 402 + reason="insufficient_points"
    （reason 而非 code：渲染器信封冲突）；槽位满 → 409 + reason="quota_exceeded"。
    通过时 hold 信息写 record.input_summary["billing_hold"]（终态对账读）。
    """
    from apps.billing.services.gate import InsufficientPoints, QuotaExceeded, dispatch_gate, release_after_dispatch
    from apps.billing.services.wallet import yuan_to_points
    from .cost_estimator import estimate_task_cost
    estimate = 0
    try:
        estimate = yuan_to_points(estimate_task_cost(record.play, TaskType.ASSISTANT_TURN)["estimated_cost"])
    except Exception:  # noqa: BLE001 — 估算失败按 0 预扣（design D3：不阻断创作）
        pass
    try:
        hold = dispatch_gate(record.play.created_by, ("task_record", record.pk),
                             estimate, slot_kind="ai")
    except InsufficientPoints as exc:
        TaskRecord.objects.filter(pk=record.pk).update(
            status=TaskStatus.FAILED,
            error_message=f"钱包余额不足：{exc}")
        return Response({"detail": f"积分余额不足，请充值后重试：{exc}",
                         "reason": "insufficient_points"},
                        status=status.HTTP_402_PAYMENT_REQUIRED)
    except QuotaExceeded as exc:
        TaskRecord.objects.filter(pk=record.pk).update(
            status=TaskStatus.FAILED,
            error_message=f"并发槽位已满：{exc}")
        return Response({"detail": f"并发任务已达上限，请稍后重试：{exc}",
                         "reason": "quota_exceeded"},
                        status=status.HTTP_409_CONFLICT)
    if hold is not None:
        summary = dict(record.input_summary or {})
        summary["billing_hold"] = {"entry_id": hold.entry.pk, "amount": hold.entry.amount}
        TaskRecord.objects.filter(pk=record.pk).update(input_summary=summary)
    release_after_dispatch(record.play.created_by, hold, "ai")
    return None


def _throttle(user):
    from apps.ai_gateway.rate_limiter import RateLimiter
    return RateLimiter().try_acquire(f"assistant-user-{user.pk}", max_rpm=MESSAGE_RPM) is not None


def _summary_attribution_record(session):
    """导航摘要 LLM 调用的费用归因记录（phase1 修复 7.2）。

    session_cost 按 ASSISTANT_TURN TaskRecord 汇总 cost——摘要调用必须挂到
    一个本会话的 record 上才会计入预算。无运行轮次时创建一个 0 成本的
    归因记录（session_id 标记），后续 CostRecord 累计到它。"""
    from ..models import TaskRecord, TaskStatus, TaskType
    running = session.running_task
    if running is not None:
        return running
    return TaskRecord.objects.create(
        play=session.play, task_type=TaskType.ASSISTANT_TURN,
        status=TaskStatus.SUCCESS, input_summary={"session_id": session.pk},
        completed_at=timezone.now())


class AssistantProposalViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = AssistantProposalSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        from .models import AssistantProposal
        qs = AssistantProposal.objects.filter(play_id__in=get_accessible_play_ids(self.request.user))
        params = self.request.query_params
        for param, field in (("play", "play_id"), ("session", "session_id")):
            value = params.get(param)
            if value:
                qs = qs.filter(**{field: value}) if str(value).isdigit() else qs.none()
        if params.get("status"):
            qs = qs.filter(status=params["status"]) if params["status"] in ProposalStatus.values else qs.none()
        return qs

    def _handle(self, fn, *args):
        proposal = self.get_object()  # 无访问权限 -> 404
        try:
            proposal = fn(proposal.pk, self.request.user, *args)
        except ProposalError as exc:
            proposal.refresh_from_db(fields=["status"])
            # 带上提案当前状态（如 STALE），前端据此切换卡片
            return Response({"detail": str(exc), "status": proposal.status}, status=exc.status_code)
        return Response(self.get_serializer(proposal).data)

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        """POST /api/assistant/proposals/{id}/apply/ {redo?: bool}"""
        redo = request.data.get("redo", False)
        if not isinstance(redo, bool):
            raise ValidationError({"redo": "应为布尔值"})
        return self._handle(apply_proposal, redo)

    @action(detail=True, methods=["post"])
    def discard(self, request, pk=None):
        return self._handle(discard_proposal)

    @action(detail=True, methods=["get"], url_path="preview")
    def preview(self, request, pk=None):
        """GET /api/assistant/proposals/{id}/preview/ -> before/after/diff/cost/stale。"""
        proposal = self.get_object()  # 无权限 -> 404
        from .preview import proposal_preview
        data = proposal_preview(proposal.pk)
        if data is None:
            return Response({"detail": "提案不存在"}, status=status.HTTP_404_NOT_FOUND)
        return Response(data)

    @action(detail=True, methods=["get"], url_path="tree")
    def tree(self, request, pk=None):
        """GET /api/assistant/proposals/{id}/tree/ -> 父链 + 子提案 + 同组备选。"""
        from .proposals import proposal_tree
        proposal = self.get_object()
        data = proposal_tree(proposal.pk)
        return Response({
            "proposal": self.get_serializer(data["proposal"]).data,
            "ancestors": self.get_serializer(data["ancestors"], many=True).data,
            "children": self.get_serializer(data["children"], many=True).data,
            "alternatives": self.get_serializer(data["alternatives"], many=True).data,
        })

    @action(detail=False, methods=["post"], url_path="compare")
    def compare(self, request):
        """POST /api/assistant/proposals/compare/ {ids: [...]} -> 并排对比。"""
        from .proposals import compare_proposals
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not 2 <= len(ids) <= 10:
            raise ValidationError({"ids": "需要 2-10 个提案 id"})
        if any(not isinstance(i, int) for i in ids):
            raise ValidationError({"ids": "id 须为整数"})
        visible = set(self.get_queryset().filter(pk__in=ids).values_list("pk", flat=True))
        if len(visible) != len(set(ids)):
            raise NotFound("提案不存在或无权访问")
        try:
            return Response(compare_proposals(ids))
        except ProposalError as exc:
            return Response({"detail": str(exc)}, status=exc.status_code)

    @action(detail=True, methods=["post"], url_path="select")
    def select(self, request, pk=None):
        """POST /api/assistant/proposals/{id}/select/ {redo?} -> 应用该备选，其余备选 STALE。"""
        from .proposals import apply_selected
        redo = request.data.get("redo", False)
        if not isinstance(redo, bool):
            raise ValidationError({"redo": "应为布尔值"})
        return self._handle(lambda pid, user: apply_selected(pid, user, redo=redo))


class AssistantSkillViewSet(viewsets.ViewSet):
    """GET /api/assistant/skills/ 列表；POST /api/assistant/skills/{name}/load/ 加载全文。

    Skills 管理（Phase 1.5）：任何登录用户可读；写路径暂未开放（文件系统管理）。
    """
    permission_classes = [IsAuthenticated]

    def list(self, request):
        from .skills import enabled, list_skills
        if not enabled():
            return Response({"skills": [], "enabled": False})
        return Response({"skills": list_skills(), "enabled": True})

    def retrieve(self, request, pk=None):
        """GET /api/assistant/skills/{name}/ -> 完整Skill（含instructions）。"""
        from .skills import enabled, get_skill, load_skill
        if not enabled():
            return Response({"detail": "Skills未启用"}, status=status.HTTP_404_NOT_FOUND)
        try:
            return Response(load_skill(pk))
        except KeyError:
            return Response({"detail": f"Skill {pk} 不存在"}, status=status.HTTP_404_NOT_FOUND)


class ScriptVersionViewSet(viewsets.ModelViewSet):
    """剧本版本分支（Phase 2.4.3）。

    GET /api/scripts/{play_id}/versions/?episode=&branch=  列表（含分支）
    POST /api/scripts/{play_id}/versions/ {episode?, branch_name, summary?}  创建快照
    POST /api/scripts/{play_id}/versions/{id}/branch/ {branch_name}  从该版本建分支
    GET  /api/scripts/{play_id}/versions/{id}/diff/?against={other_id}  结构化对比
    POST /api/scripts/{play_id}/versions/merge/ {base, ours, theirs, branch_name, resolutions?}
    """
    permission_classes = [IsAuthenticated]
    serializer_class = ScriptVersionSerializer
    http_method_names = ["get", "post", "head", "options"]

    def _play(self):
        return _play_or_404(self.request.user, self.kwargs["play_id"])

    def get_queryset(self):
        from .models import ScriptVersion
        play = self._play()
        qs = ScriptVersion.objects.filter(play=play).select_related("episode")
        episode = self.request.query_params.get("episode")
        if episode:
            qs = qs.filter(episode__episode_number=episode) if str(episode).isdigit() else qs.none()
        branch = self.request.query_params.get("branch")
        if branch:
            qs = qs.filter(branch_name=branch)
        return qs.order_by("branch_name", "-version")

    def _episode(self, play, number):
        from ..models import Episode
        if number in (None, ""):
            return None
        if not str(number).isdigit():
            raise ValidationError({"episode": "集数须为数字"})
        episode = Episode.objects.filter(play=play, episode_number=int(number)).first()
        if episode is None:
            raise ValidationError({"episode": f"第 {number} 集不存在"})
        return episode

    def list(self, request, play_id=None):
        from .script_versions import branches
        play = self._play()
        episode = self._episode(play, request.query_params.get("episode"))
        latest = branches(play, episode)
        return Response({
            "branches": [{"branch_name": name, "version": v.version, "id": v.pk,
                          "created_at": v.created_at, "summary": v.summary}
                         for name, v in latest.items()],
            "versions": self.get_serializer(self.get_queryset(), many=True).data,
        })

    def create(self, request, play_id=None):
        from .script_versions import VersionError, create_version
        play = self._play()
        if not can_write_play(request.user, play):
            raise PermissionDenied("对该短剧只有只读权限")
        episode = self._episode(play, request.data.get("episode"))
        try:
            version = create_version(play, episode=episode,
                                     branch_name=request.data.get("branch_name") or "main",
                                     summary=request.data.get("summary") or "",
                                     user=request.user)
        except VersionError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response(self.get_serializer(version).data, status=status.HTTP_201_CREATED)

    def _version(self, play):
        from .models import ScriptVersion
        version = ScriptVersion.objects.filter(pk=self.kwargs["pk"], play=play).first()
        if version is None:
            raise NotFound("版本不存在")
        return version

    @action(detail=True, methods=["post"])
    def branch(self, request, play_id=None, pk=None):
        from .script_versions import VersionError, create_branch
        play = self._play()
        if not can_write_play(request.user, play):
            raise PermissionDenied("对该短剧只有只读权限")
        parent = self._version(play)
        name = (request.data.get("branch_name") or "").strip()
        if not name:
            raise ValidationError({"branch_name": "分支名不能为空"})
        try:
            version = create_branch(play, from_version=parent, branch_name=name,
                                    user=request.user, summary=request.data.get("summary") or "")
        except VersionError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response(self.get_serializer(version).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def diff(self, request, play_id=None, pk=None):
        from .script_versions import script_diff
        play = self._play()
        ours = self._version(play)
        against = request.query_params.get("against")
        if not against or not str(against).isdigit():
            raise ValidationError({"against": "需要对比版本 id"})
        from .models import ScriptVersion
        other = ScriptVersion.objects.filter(pk=int(against), play=play).first()
        if other is None:
            raise NotFound("对比版本不存在")
        return Response(script_diff(other, ours))

    @action(detail=False, methods=["post"], url_path="merge")
    def merge(self, request, play_id=None):
        from .models import ScriptVersion
        from .script_versions import VersionError, merge_to_new_version
        play = self._play()
        if not can_write_play(request.user, play):
            raise PermissionDenied("对该短剧只有只读权限")
        ids = [request.data.get(k) for k in ("base", "ours", "theirs")]
        if any(not isinstance(i, int) for i in ids):
            raise ValidationError({"detail": "base / ours / theirs 均需版本 id"})
        versions = {v.pk: v for v in ScriptVersion.objects.filter(pk__in=ids, play=play)}
        if len(versions) != 3:
            raise NotFound("版本不存在或不属于该短剧")
        resolutions = request.data.get("resolutions") or {}
        if not isinstance(resolutions, dict):
            raise ValidationError({"resolutions": "须为对象"})
        try:
            merged = merge_to_new_version(play, versions[ids[0]], versions[ids[1]], versions[ids[2]],
                                          branch_name=request.data.get("branch_name") or "merge",
                                          user=request.user, resolutions=resolutions)
        except VersionError as exc:
            return Response({"detail": str(exc)}, status=exc.status_code)
        return Response(self.get_serializer(merged).data, status=status.HTTP_201_CREATED)


class CharacterVersionViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """角色版本（Phase 2.4.3.5）。

    GET  /api/characters/{character_id}/versions/
    POST /api/characters/{character_id}/versions/  记录当前设定快照
    """
    permission_classes = [IsAuthenticated]
    serializer_class = CharacterVersionSerializer

    def get_character(self):
        from ..models import Character
        character = Character.objects.filter(pk=self.kwargs["character_id"]).select_related("play").first()
        if character is None or character.play_id not in set(get_accessible_play_ids(self.request.user)):
            raise NotFound("角色不存在或无权访问")
        return character

    def get_queryset(self):
        from .models import CharacterVersion
        return CharacterVersion.objects.filter(character=self.get_character()).order_by("-version")

    def create(self, request, character_id=None):
        from .script_versions import record_character_version
        character = self.get_character()
        if not can_write_play(request.user, character.play):
            raise PermissionDenied("对该短剧只有只读权限")
        version = record_character_version(character, changed_fields=request.data.get("changed_fields"),
                                           user=request.user)
        return Response(self.get_serializer(version).data, status=status.HTTP_201_CREATED)


class AssistantSkillLibraryViewSet(viewsets.ModelViewSet):
    """技能库管理（Phase 2.2）。

    GET/POST /api/assistant/skill-library/
    GET/PUT/PATCH/DELETE /api/assistant/skill-library/{id}/
    POST /api/assistant/skill-library/{id}/archive/
    读写需短剧写权限（PLAY 作用域）或管理员（GLOBAL 作用域）；列表按可访问短剧过滤。
    """
    permission_classes = [IsAuthenticated]
    serializer_class = AssistantSkillLibrarySerializer
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def get_queryset(self):
        from django.db.models import Q

        from .models import SkillLibrary
        user = self.request.user
        qs = SkillLibrary.objects.select_related("play").order_by("-confidence", "-success_count", "name")
        if not _is_admin(user):
            # GLOBAL 技能 play 为 NULL，play_id IN (...) 不匹配 NULL 行——须显式 OR 进来，
            # 否则普通用户看不到运行时 system prompt 里被注入的全局技能
            qs = qs.filter(Q(play_id__in=get_accessible_play_ids(user)) | Q(play__isnull=True))
        play = self.request.query_params.get("play")
        if play:
            qs = qs.filter(play_id=play) if str(play).isdigit() else qs.none()
        active = self.request.query_params.get("active")
        if active in ("0", "1"):
            qs = qs.filter(is_active=active == "1")
        return qs

    def _check_write(self, play):
        if play is None:
            if not _is_admin(self.request.user):
                raise PermissionDenied("仅管理员可维护全局技能")
            return
        if not can_write_play(self.request.user, play):
            raise PermissionDenied("对该短剧只有只读权限，不能维护技能库")

    def perform_create(self, serializer):
        from .models import SkillLibrary, SkillScope
        play = serializer.validated_data.get("play")
        # 显式给了 play 未给 scope 时默认 PLAY，避免建出"带 play 的全局技能"
        scope = serializer.validated_data.get("scope") or (
            SkillScope.PLAY if play is not None else SkillScope.GLOBAL)
        if scope == SkillScope.GLOBAL:
            play = None
        if scope == SkillScope.PLAY and play is None:
            raise ValidationError({"play": "PLAY 作用域必须指定短剧"})
        self._check_write(play)
        if SkillLibrary.objects.filter(scope=scope, play=play,
                                       name=serializer.validated_data["name"]).exists():
            raise ValidationError({"name": "该作用域下技能名已存在"})
        serializer.save(scope=scope, play=play)

    def perform_update(self, serializer):
        skill = self.get_object()
        self._check_write(skill.play)
        new_play = serializer.validated_data.get("play", skill.play)
        new_scope = serializer.validated_data.get("scope", skill.scope)
        if new_scope == "GLOBAL":
            new_play = None
        if new_scope == "PLAY" and new_play is None:
            raise ValidationError({"play": "PLAY 作用域必须指定短剧"})
        self._check_write(new_play)
        serializer.save(scope=new_scope, play=new_play)

    def perform_destroy(self, instance):
        self._check_write(instance.play)
        instance.delete()

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        skill = self.get_object()
        self._check_write(skill.play)
        skill.is_active = False
        skill.save(update_fields=["is_active", "updated_at"])
        return Response(AssistantSkillLibrarySerializer(skill).data)


class AssistantExperimentViewSet(viewsets.ModelViewSet):
    """A/B 策略实验（Phase 2.2）。

    GET/POST /api/assistant/experiments/
    GET/PATCH /api/assistant/experiments/{id}/
    GET /api/assistant/experiments/{id}/report/
    POST /api/assistant/experiments/{id}/outcome/ {bucket_key} 或 {variant_key, success}
    """
    permission_classes = [IsAuthenticated]
    serializer_class = AssistantExperimentSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        from .models import StrategyExperiment
        qs = StrategyExperiment.objects.all()
        if not _is_admin(self.request.user):
            qs = qs.filter(created_by=self.request.user)
        return qs

    def perform_create(self, serializer):
        from .learning import normalize_variants
        variants = normalize_variants(serializer.validated_data.get("variants"))
        if len(variants) < 2:
            raise ValidationError({"variants": "至少需要两个变体"})
        serializer.save(variants=variants, created_by=self.request.user)

    def perform_update(self, serializer):
        from .learning import normalize_variants
        if "variants" in serializer.validated_data:
            variants = normalize_variants(serializer.validated_data["variants"])
            if len(variants) < 2:
                raise ValidationError({"variants": "至少需要两个变体"})
            serializer.save(variants=variants)
        else:
            serializer.save()

    @action(detail=True, methods=["get"])
    def report(self, request, pk=None):
        from .learning import cost_comparison_report, experiment_report
        experiment = self.get_object()
        # 成本对比实验（主计划 3.3.11）：variant 带 cost_record_ids 时输出费用均值对比
        has_cost = any(v.get("cost_record_ids")
                       for v in (experiment.variants or []) if isinstance(v, dict))
        if has_cost:
            return Response(cost_comparison_report(experiment))
        return Response(experiment_report(experiment))

    @action(detail=True, methods=["post"])
    def outcome(self, request, pk=None):
        from .learning import assign_variant, finish_if_conclusive, record_outcome
        experiment = self.get_object()
        # 缺省保持旧的 False 语义；显式传了非布尔/非 true|false 字符串才 400
        success_raw = request.data.get("success", False)
        if not isinstance(success_raw, (bool, str)) or (
                isinstance(success_raw, str) and success_raw.strip().lower() not in ("true", "false")):
            raise ValidationError({"success": "应为布尔值（true/false）"})
        success = _strict_bool(success_raw)
        variant_key = request.data.get("variant_key")
        if not variant_key:
            bucket_key = request.data.get("bucket_key")
            if bucket_key is None:
                raise ValidationError({"bucket_key": "需要 variant_key 或 bucket_key"})
            variant = assign_variant(experiment, str(bucket_key))
            if variant is None:
                raise ValidationError({"detail": "实验没有可用变体"})
            variant_key = variant["key"]
        try:
            record_outcome(experiment, str(variant_key), success=success)
        except KeyError as exc:
            raise ValidationError({"variant_key": str(exc)}) from exc
        return Response(finish_if_conclusive(experiment))


class AssistantReviewViewSet(viewsets.ViewSet):
    """GET /api/assistant/review/report/?play_id=&episode= -> 确定性+主观合并审查报告。"""
    permission_classes = [IsAuthenticated]

    def list(self, request):
        from ..models import Episode
        from .agent_review import review_report
        from .models import AssistantSession
        play_id = request.query_params.get("play_id") or request.query_params.get("play")
        ep_num = request.query_params.get("episode")
        if not play_id or not str(play_id).isdigit():
            raise ValidationError({"play_id": "必填且为数字"})
        if not ep_num or not str(ep_num).isdigit():
            raise ValidationError({"episode": "必填且为集数"})
        play = _play_or_404(request.user, play_id)
        episode = Episode.objects.filter(play=play, episode_number=int(ep_num)).first()
        if episode is None:
            return Response({"detail": f"第 {ep_num} 集不存在"},
                            status=status.HTTP_404_NOT_FOUND)
        # 审查会触发 LLM 花费：按用户限流，防止只读成员无节制烧钱
        if not _throttle(request.user):
            return Response({"detail": f"审查过于频繁（每分钟最多 {MESSAGE_RPM} 次）"},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        # 需要本会话以取 agent_config 做 LLM 审查；无会话时用 play.agent_config
        session = (AssistantSession.objects.filter(user=request.user, play=play)
                   .select_related("agent_config").first())
        if session is None:
            # 伪造一个轻量会话对象供 reviewer 取配置（不落库）
            session = AssistantSession(user=request.user, play=play,
                                       agent_config=play.agent_config)
        try:
            return Response(review_report(session, episode))
        except Exception as exc:  # noqa: BLE001
            return Response({"detail": f"审查失败: {exc}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AssistantCostEstimateViewSet(viewsets.ViewSet):
    """POST /api/assistant/cost/estimate/ {play_id, action?, target_id?, task_type?}
    -> 估算生成类操作的费用。action ∈ {redo_shot, redo_scene, preview_episode}；
    task_type ∈ TaskType 通用类型。"""
    permission_classes = [IsAuthenticated]

    def create(self, request):
        from .cost_estimator import (CostEstimateError, estimate_generation_cost,
                                     estimate_task_cost, budget_remaining)
        from .models import AssistantSession
        play_id = request.data.get("play_id") or request.data.get("play")
        if not play_id or not str(play_id).isdigit():
            raise ValidationError({"play_id": "必填且为数字"})
        play = _play_or_404(request.user, play_id)

        action = request.data.get("action")
        target_id = request.data.get("target_id")
        task_type = request.data.get("task_type")
        session_id = request.data.get("session_id")

        if action:
            if not target_id or not str(target_id).isdigit():
                raise ValidationError({"target_id": "action 估算必填且为数字"})
            try:
                result = estimate_generation_cost(play, action, int(target_id))
            except CostEstimateError as exc:
                # phase1 修复 10.2：业务非法请求 4xx，而非 200 零费用
                raise ValidationError({"detail": str(exc)}) from exc
        elif task_type:
            result = estimate_task_cost(play, task_type)
        else:
            raise ValidationError({"detail": "action 或 task_type 必填其一"})

        # 附会话预算余额（若提供 session_id 且为本人会话）
        if session_id and str(session_id).isdigit():
            sess = AssistantSession.objects.filter(
                pk=int(session_id), user=request.user).first()
            if sess is not None:
                rem = budget_remaining(sess)
                result["budget_remaining"] = None if rem is None else str(rem)
        return Response(result)


class AssistantEvaluationViewSet(viewsets.ViewSet):
    """GET /api/assistant/evaluation/?play=&period=7d -> 聚合评估指标 + 趋势。"""
    permission_classes = [IsAuthenticated]

    def list(self, request):
        from .evaluation import aggregate_metrics
        play_id = request.query_params.get("play")
        period = request.query_params.get("period", "7d")
        try:
            days = int(period.rstrip("d"))
        except ValueError:
            days = 7
        days = max(1, min(days, 3650))
        # play 过滤先过成员权限（指定了不可访问的 play 视为不存在）
        pid = int(play_id) if play_id and str(play_id).isdigit() else None
        if pid is not None:
            _play_or_404(request.user, pid)
        # 未指定 play 时聚合范围收敛到当前用户可访问短剧（防跨租户泄漏）
        accessible = get_accessible_play_ids(request.user)
        return Response(aggregate_metrics(pid, days, accessible_play_ids=accessible))


class AssistantMemoryViewSet(viewsets.ViewSet):
    """GET/PUT /api/assistant/memories/{play_id}/：短剧成员可读，写权限可改，需带 revision（冲突 409）。"""
    permission_classes = [IsAuthenticated]

    def _play(self, request, pk):
        return _play_or_404(request.user, pk)

    def retrieve(self, request, pk=None):
        from .project_memory import load_memory
        return Response(load_memory(self._play(request, pk)))

    def update(self, request, pk=None):
        from .project_memory import MemoryConflict, MemoryTooLarge, load_memory, save_memory
        play = self._play(request, pk)
        if not can_write_play(request.user, play):
            raise PermissionDenied("无权修改该短剧的助手记忆")
        content, revision = request.data.get("content"), request.data.get("revision")
        if not isinstance(content, str) or not isinstance(revision, str):
            raise ValidationError({"detail": "content 与 revision 必填且为字符串"})
        try:
            return Response(save_memory(play, content, revision, request.user))
        except MemoryConflict as exc:
            return Response({"detail": str(exc), **load_memory(play)}, status=status.HTTP_409_CONFLICT)
        except MemoryTooLarge as exc:
            raise ValidationError({"content": str(exc)}) from exc
        except ValueError as exc:
            raise ValidationError({"content": str(exc)}) from exc


class AssistantToolViewSet(viewsets.ViewSet):
    """工具注册表管理（Phase 3.1 轨道 2 / registry D6）。

    - GET /：管理员见全部；普通用户见自己的 + verified OFFICIAL
    - POST /：创建（普通用户强制 CUSTOM；非管理员请求 OFFICIAL → 403）
    - PATCH /{id}/：更新（作者/管理员，乐观并发 expected_version）
    - DELETE /{id}/：删除（作者/管理员）
    - POST /{id}/activate|deactivate/：启停（作者/管理员）
    - POST /{id}/verify/：验证（仅平台管理员）
    - POST /{id}/try/：试运行（作者/管理员，沙箱超时减半 + 限流）
    开关关闭时 list 返回空并标记未启用（对齐 skills 形状），其余动作 404。
    """
    permission_classes = [IsAuthenticated]

    def _enabled(self):
        from . import registry
        return registry._enabled()

    @staticmethod
    def _avg_rating(record):
        """评分聚合回显（rate 响应附均分；市场列表在 marketplace 端点内聚合）。"""
        from django.db.models import Avg
        agg = record.ratings.aggregate(avg=Avg("rating"))
        return f"{agg['avg']:.2f}" if agg["avg"] is not None else None

    def _get_record(self, request, pk, *, require_manage=False):
        from .models import ToolRegistry
        record = ToolRegistry.objects.filter(pk=pk).first()
        if record is None:
            raise NotFound
        is_admin = bool(is_platform_admin(request.user))
        is_author = record.author_id == request.user.pk
        if require_manage and not (is_admin or is_author):
            raise PermissionDenied("仅工具作者或平台管理员可操作")
        return record, is_admin, is_author

    def list(self, request):
        from django.db.models import Q
        from .models import ToolRegistry, ToolType
        from .serializers import ToolRegistrySerializer
        if not self._enabled():
            return Response({"tools": [], "enabled": False})
        q = Q(author=request.user) | Q(tool_type=ToolType.OFFICIAL, is_verified=True)
        if is_platform_admin(request.user):
            records = ToolRegistry.objects.all()
        else:
            records = ToolRegistry.objects.filter(q)
        data = [ToolRegistrySerializer(r, context={"is_author": r.author_id == request.user.pk,
                                                   "is_admin": False}).data
                for r in records]
        return Response({"tools": data, "enabled": True})

    def create(self, request):
        from . import registry
        from .models import ToolType
        from .serializers import ToolRegistrySerializer
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        if not _throttle(request.user):
            return Response({"detail": "操作过于频繁，请稍后再试"},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        tool_type = request.data.get("tool_type") or ToolType.CUSTOM
        if tool_type == ToolType.OFFICIAL and not is_platform_admin(request.user):
            raise PermissionDenied("仅平台管理员可创建官方工具")
        try:
            record = registry.create_tool(
                tool_name=request.data.get("tool_name"),
                tool_type=tool_type,
                category=request.data.get("category") or "",
                description=request.data.get("description"),
                input_schema=request.data.get("input_schema") or {},
                handler_code=request.data.get("handler_code"),
                author=request.user,
            )
        except registry.RegistryValidationError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(ToolRegistrySerializer(record, context={"is_author": True,
                                                                "is_admin": False}).data,
                        status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        from . import registry
        from .serializers import ToolRegistrySerializer
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        record, is_admin, _ = self._get_record(request, pk, require_manage=True)
        expected = request.data.get("expected_version")
        if expected is None or not isinstance(expected, int):
            raise ValidationError({"expected_version": "必填且为整数"})
        fields = {}
        for key in ("tool_name", "category", "description", "input_schema", "handler_code"):
            if key in request.data:
                fields[key] = request.data[key]
        if not fields:
            raise ValidationError({"detail": "无可更新字段"})
        try:
            record = registry.update_tool(record, expected_version=expected, **fields)
        except registry.RegistryValidationError as exc:
            if "版本冲突" in str(exc):
                return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
            raise ValidationError({"detail": str(exc)})
        return Response(ToolRegistrySerializer(record, context={"is_author": True,
                                                                "is_admin": is_admin}).data)

    def destroy(self, request, pk=None):
        from .models import ToolRegistry
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        record, _, _ = self._get_record(request, pk, require_manage=True)
        ToolRegistry.objects.filter(pk=record.pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        return self._toggle(request, pk, True)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        return self._toggle(request, pk, False)

    def _toggle(self, request, pk, value):
        from . import registry
        from .models import ToolRegistry
        from .serializers import ToolRegistrySerializer
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        record, is_admin, _ = self._get_record(request, pk, require_manage=True)
        ToolRegistry.objects.filter(pk=record.pk).update(is_active=value)
        registry._cache.invalidate(record.tool_name)
        record.refresh_from_db()
        return Response(ToolRegistrySerializer(record, context={"is_author": True,
                                                                "is_admin": is_admin}).data)

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        """验证（审核流扩展：接受可选 note，动作落审计日志）。"""
        from . import registry
        from .models import ToolRegistry, ToolReviewAction
        from .serializers import ToolRegistrySerializer
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        if not is_platform_admin(request.user):
            raise PermissionDenied("仅平台管理员可验证工具")
        record = ToolRegistry.objects.filter(pk=pk).first()
        if record is None:
            raise NotFound
        note = request.data.get("note") or ""
        try:
            registry.review_action(record, actor=request.user,
                                    action=ToolReviewAction.VERIFY, note=note)
        except registry.RegistryValidationError as exc:
            raise ValidationError({"detail": str(exc)})
        record.refresh_from_db()
        return Response(ToolRegistrySerializer(record, context={"is_author": False,
                                                                "is_admin": True}).data)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        """送审（审核流）：作者对未验证条目表达审核意图，落 SUBMIT 审计。"""
        from . import registry
        from .models import ToolReviewAction
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        record, _, _ = self._get_record(request, pk, require_manage=True)
        try:
            registry.review_action(record, actor=request.user,
                                    action=ToolReviewAction.SUBMIT,
                                    note=request.data.get("note") or "")
        except registry.RegistryValidationError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"detail": "已送审", "target": record.pk},
                        status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """驳回（仅平台管理员，note 必填）：停用条目并回显驳回原因给作者。"""
        from . import registry
        from .models import ToolReviewAction
        from .serializers import ToolRegistrySerializer
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        if not is_platform_admin(request.user):
            raise PermissionDenied("仅平台管理员可审核")
        record, is_admin, is_author = self._get_record(request, pk)
        try:
            registry.review_action(record, actor=request.user,
                                    action=ToolReviewAction.REJECT,
                                    note=request.data.get("note") or "")
        except registry.ReviewStateError as exc:
            raise ValidationError({"detail": str(exc)})
        except registry.RegistryValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        record.refresh_from_db()
        return Response(ToolRegistrySerializer(record, context={
            "is_author": is_author, "is_admin": is_admin,
            "show_review_note": is_author or is_admin}).data)

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        """下架（仅平台管理员，note 必填）：清 verified + 停用 + 缓存失效。"""
        from . import registry
        from .models import ToolReviewAction
        from .serializers import ToolRegistrySerializer
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        if not is_platform_admin(request.user):
            raise PermissionDenied("仅平台管理员可审核")
        record, is_admin, is_author = self._get_record(request, pk)
        try:
            registry.review_action(record, actor=request.user,
                                    action=ToolReviewAction.REVOKE,
                                    note=request.data.get("note") or "")
        except registry.ReviewStateError as exc:
            raise ValidationError({"detail": str(exc)})
        except registry.RegistryValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        record.refresh_from_db()
        return Response(ToolRegistrySerializer(record, context={
            "is_author": is_author, "is_admin": is_admin,
            "show_review_note": is_author or is_admin}).data)

    @action(detail=False, methods=["get"], url_path="reviews")
    def review_queue(self, request):
        """审核队列（仅平台管理员）：最新动作为 SUBMIT 且无终局动作的条目。"""
        from . import registry
        from .serializers import ToolRegistrySerializer
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        if not is_platform_admin(request.user):
            raise PermissionDenied("仅平台管理员可见审核队列")
        data = []
        for record in registry.pending_review_tools():
            submit_log = record.review_logs.filter(
                action="SUBMIT").order_by("-id").first()
            entry = ToolRegistrySerializer(record, context={"is_author": False,
                                                            "is_admin": True}).data
            entry["submitted_at"] = submit_log.created_at if submit_log else None
            entry["submit_note"] = submit_log.note if submit_log else ""
            data.append(entry)
        return Response({"tools": data})

    @action(detail=False, methods=["get"], url_path="marketplace")
    def marketplace(self, request):
        """市场列表（design D1）：verified OFFICIAL 目录 + 评分聚合 + 安装计数。"""
        from django.db.models import Avg, Count, Q
        from .models import ToolRegistry, ToolType
        from .serializers import ToolRegistrySerializer
        if not self._enabled():
            return Response({"tools": [], "enabled": False})
        qs = (ToolRegistry.objects
              .filter(tool_type=ToolType.OFFICIAL, is_verified=True)
              .annotate(avg_rating=Avg("ratings__rating"),
                        rating_count=Count("ratings", distinct=True),
                        install_count=Count("installed_copies", distinct=True)))
        category = (request.query_params.get("category") or "").strip()
        if category:
            qs = qs.filter(category=category)
        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(tool_name__icontains=q) | Q(description__icontains=q))
        data = []
        for record in qs:
            entry = ToolRegistrySerializer(record, context={
                "is_author": record.author_id == request.user.pk,
                "is_admin": False}).data
            entry["avg_rating"] = (f"{record.avg_rating:.2f}"
                                   if record.avg_rating is not None else None)
            entry["rating_count"] = record.rating_count
            entry["install_count"] = record.install_count
            entry["has_docs"] = bool(isinstance(record.input_schema, dict)
                                     and record.input_schema)
            data.append(entry)
        return Response({"tools": data, "enabled": True})

    @action(detail=True, methods=["post"], url_path="marketplace/install")
    def install(self, request, pk=None):
        """复制式安装（design D1）：市场条目 → 用户本地 CUSTOM 副本。"""
        from . import registry
        from .models import ToolRegistry
        from .serializers import ToolRegistrySerializer
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        source = ToolRegistry.objects.filter(pk=pk).first()
        if source is None:
            raise NotFound
        try:
            record, created = registry.install_tool(source, user=request.user)
        except registry.RegistryValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({
            "installed": created,
            "id": record.pk,
            "tool": ToolRegistrySerializer(record, context={"is_author": True,
                                                            "is_admin": False}).data,
        }, status=(status.HTTP_201_CREATED if created else status.HTTP_200_OK))

    @action(detail=True, methods=["post"], url_path="rate")
    def rate(self, request, pk=None):
        """评分（design D3）：1-5 分 + 可选评论；upsert 覆盖；不可评返回 404。"""
        from . import registry
        from .models import ToolRegistry
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        record = ToolRegistry.objects.filter(pk=pk).first()
        if record is None:
            raise NotFound
        if not registry.visible_tools(request.user).filter(pk=record.pk).exists():
            raise NotFound
        if record.author_id == request.user.pk:
            raise ValidationError({"detail": "不能评自己的工具"})
        if not _throttle(request.user):
            return Response({"detail": "操作过于频繁，请稍后再试"},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        rating = request.data.get("rating")
        if not isinstance(rating, int):
            raise ValidationError({"rating": "必填且为整数"})
        try:
            obj = registry.rate_tool(record, user=request.user, rating=rating,
                                     comment=request.data.get("comment") or "")
        except registry.RegistryValidationError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"rating": obj.rating, "comment": obj.comment,
                         "avg_rating": self._avg_rating(record)})

    @action(detail=True, methods=["get"], url_path="docs")
    def docs(self, request, pk=None):
        """文档自动生成（design D5）：确定性 Markdown，不含 handler 源码。"""
        from . import registry
        from .models import ToolRegistry, ToolType
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        record = ToolRegistry.objects.filter(pk=pk).first()
        if record is None:
            raise NotFound
        is_admin = bool(is_platform_admin(request.user))
        is_author = record.author_id == request.user.pk
        is_market = (record.tool_type == ToolType.OFFICIAL and record.is_verified)
        if not (is_admin or is_author or is_market):
            raise NotFound  # 不泄露他人未验证工具的存在性
        return Response({"doc": registry.render_docs(record)})

    @action(detail=True, methods=["post"])
    def try_run(self, request, pk=None):
        """试运行（registry D3）：样例入参走同一沙箱，超时/上限减半。仅作者/管理员。"""
        from . import registry
        if not self._enabled():
            raise NotFound("工具注册表未启用")
        record, _, _ = self._get_record(request, pk, require_manage=True)
        if not _throttle(request.user):
            return Response({"detail": "试运行过于频繁，请稍后再试"},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        args = request.data.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ValidationError({"args": "应为 JSON 对象"})
        try:
            result = registry.run_in_sandbox(
                record, args,
                timeout_s=registry.sandbox_try_timeout(),
                output_limit=registry._sandbox_output_limit() // 2)
        except registry.SandboxError as exc:
            return Response({"ok": False, "error": str(exc)}, status=status.HTTP_200_OK)
        return Response({"ok": True, "result": result})


class InspirationViewSet(viewsets.ModelViewSet):
    """灵感库管理（Phase 3.5.1 / design D3）。

    GET/POST /api/inspirations/（?play=&category=&q=）；GET/PUT/PATCH/DELETE /{id}/。
    可见性：GLOBAL 全员可读；PLAY 级本剧成员可读。写删：本人或本剧写权限
    （GLOBAL 条目限创建者与平台管理员）。REST 不受 ENABLE_INSPIRATION_LIBRARY
    开关限制（开关只控 agent 工具面，与 skills API 同型）。
    """
    permission_classes = [IsAuthenticated]
    serializer_class = InspirationSerializer
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def get_queryset(self):
        from django.db.models import Q
        from .models import Inspiration
        user = self.request.user
        qs = Inspiration.objects.select_related("play")
        if not _is_admin(user):
            qs = qs.filter(Q(play_id__in=get_accessible_play_ids(user))
                           | Q(play__isnull=True))
        play = self.request.query_params.get("play")
        if play:
            qs = qs.filter(play_id=play) if str(play).isdigit() else qs.none()
        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)
        q = (self.request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(content__icontains=q) | Q(category__icontains=q))
        return qs

    def _check_write(self, inspiration=None, play=None):
        """写权限：目标条目本人/本剧写权限；新条目按 play 判（GLOBAL 限管理员）。"""
        if inspiration is not None:
            if inspiration.user_id == self.request.user.pk:
                return
            play = inspiration.play
        if play is None:
            if not _is_admin(self.request.user):
                raise PermissionDenied("仅平台管理员可维护全局灵感")
            return
        from ..models import Play
        play_obj = Play.objects.filter(pk=play.pk).first()
        if play_obj is None or not can_write_play(self.request.user, play_obj):
            raise PermissionDenied("对该短剧只有只读权限，不能维护灵感")

    def perform_create(self, serializer):
        play = serializer.validated_data.get("play")
        if play is not None:
            from ..models import Play
            play_obj = Play.objects.filter(pk=play.pk).first()
            if play_obj is None or not (can_write_play(self.request.user, play_obj)
                                        or _is_admin(self.request.user)):
                raise PermissionDenied("对该短剧只有只读权限，不能创建灵感")
        else:
            if not _is_admin(self.request.user):
                raise PermissionDenied("仅平台管理员可创建全局灵感")
        serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        self._check_write(inspiration=self.get_object())
        serializer.save()

    def perform_destroy(self, instance):
        self._check_write(inspiration=instance)
        instance.delete()


class AssistantReviewFeedbackViewSet(viewsets.ViewSet):
    """审查反馈（Phase 3.5.2 / design D4）。

    POST /api/assistant/review/feedback/ {episode, rating, issues_feedback?, review_log?}
    GET /api/assistant/review/feedback/trends/?play_id=
    """
    permission_classes = [IsAuthenticated]

    def _episode_or_403(self, request, episode_id):
        from ..models import Episode
        if not episode_id or not str(episode_id).isdigit():
            raise ValidationError({"episode": "必填且为数字"})
        episode = Episode.objects.select_related("play").filter(pk=int(episode_id)).first()
        if episode is None:
            return None, None
        if episode.play_id not in get_accessible_play_ids(request.user):
            raise PermissionDenied("仅本剧成员可提交审查反馈")
        return episode, episode.play

    def create(self, request):
        from django.conf import settings
        from .models import ReviewFeedback
        if not getattr(settings, "ENABLE_AGENT_REVIEW", False):
            raise NotFound("Agent 审查未启用")
        episode, _ = self._episode_or_403(request, request.data.get("episode"))
        if episode is None:
            return Response({"detail": "分集不存在"}, status=status.HTTP_404_NOT_FOUND)
        rating = request.data.get("rating")
        if not isinstance(rating, int) or isinstance(rating, bool) or not (1 <= rating <= 5):
            raise ValidationError({"rating": "必填且为 1-5 的整数"})
        review_log = request.data.get("review_log") or None
        if review_log is not None:
            from ..models import ReviewLog
            review_log = (ReviewLog.objects.filter(episode=episode, pk=review_log).first()
                          if str(review_log).isdigit() else None)
        serializer = ReviewFeedbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        obj, _ = ReviewFeedback.objects.update_or_create(
            episode=episode, user=request.user, review_log=review_log,
            defaults={"rating": rating,
                      "issues_feedback": serializer.validated_data["issues_feedback"]})
        return Response(ReviewFeedbackSerializer(obj).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="trends")
    def trends(self, request):
        """反馈趋势（design D4）：按维度聚合全剧 agree/disagree + 最近 10 条。"""
        from django.conf import settings
        from django.utils import timezone as dj_tz
        from datetime import timedelta
        from ..models import Episode, ReviewLog
        from .models import ReviewFeedback
        if not getattr(settings, "ENABLE_AGENT_REVIEW", False):
            raise NotFound("Agent 审查未启用")
        play_id = request.query_params.get("play_id")
        if not play_id or not str(play_id).isdigit():
            raise ValidationError({"play_id": "必填且为数字"})
        play = _play_or_404(request.user, play_id)
        window = int(getattr(settings, "REVIEW_FEEDBACK_WINDOW_DAYS", 30))
        since = dj_tz.now() - timedelta(days=window)
        rows = (ReviewFeedback.objects
                .filter(episode__play=play, created_at__gte=since)
                .order_by("-created_at"))
        dims = {}
        recent = []
        for fb in rows[:200]:
            for item in (fb.issues_feedback or []):
                dim = str(item.get("issue_key", "")).split("-")[0] or "unknown"
                agg = dims.setdefault(dim, {"agree": 0, "disagree": 0})
                if item.get("agree") is True:
                    agg["agree"] += 1
                elif item.get("agree") is False:
                    agg["disagree"] += 1
        for fb in rows[:10]:
            recent.append({"episode": fb.episode_id, "rating": fb.rating,
                           "issues_feedback": fb.issues_feedback,
                           "created_at": fb.created_at})
        return Response({"play": play.pk, "window_days": window,
                         "dimensions": dims, "recent": recent})


class AssistantTrustViewSet(viewsets.ViewSet):
    """GET /api/assistant/trust/（主计划 3.2.5）：本人信任分级。

    返回 {trust_score, auto_approve_threshold, level}；无记录时返回默认初值
    （不落库）。写侧仅内部累积（apply/discard 事件驱动），无手工设置端点。
    """
    permission_classes = [IsAuthenticated]

    def list(self, request):
        from .trust import effective_threshold, level_label, trust_level_of
        row = trust_level_of(request.user)
        score = float(row.trust_score) if row is not None else 0.0
        return Response({
            "trust_score": score,
            "auto_approve_threshold": effective_threshold(row),
            "level": level_label(score),
        })


class AssistantRealtimeConfigViewSet(viewsets.ViewSet):
    """GET /api/assistant/realtime/config/（主计划 3.2.12）：降级判据端点。

    返回 {enabled, mode}：enabled=ENABLE_REALTIME_EVENTS；mode = enabled ?
    "ws" : "polling"。前端启动探测：enabled 先试 WS（失败回退长轮询），否则
    直接长轮询。对所有认证用户可用，不受实时通道开关限制。
    """
    permission_classes = [IsAuthenticated]

    def list(self, request):
        from django.conf import settings
        enabled = bool(getattr(settings, "ENABLE_REALTIME_EVENTS", False))
        return Response({"enabled": enabled, "mode": "ws" if enabled else "polling"})
