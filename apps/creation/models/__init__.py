"""creation 应用模型包。

按资源分文件：play / asset / agent / operations；助手模型在 assistant/。
所有模型与枚举在此 re-export，便于 `from apps.creation.models import X`。
"""
from .play import (
    AuditMode, Character, CharacterRole, ConsistencyMode, Episode, EpisodeStatus,
    Gender, Play, PlayStatus, RedoGranularity, Scene, SceneStatus, Shot, ShotStatus,
    ShotType, TimeOfDay, VoiceProfile,
)
from .asset import (
    Asset, AssetDependency, AssetLibrary, AssetStatus, AssetType, RelationType,
)
from .agent import AgentConfig
from .membership import (
    AuditLog, InvitationStatus, MemberRole, MemberStatus, PlayInvitation, PlayMember,
)
from .operations import (
    ExecutionClaimStatus, LocalTaskExecution, ReviewDecision, ReviewLog, ReviewPoint,
    TaskRecord, TaskStatus, TaskType,
)
from .novel import (
    Canvas, CanvasNode, CanvasNodeType, ChapterSnapshot, Clip, Composition,
    DirectorElement, DirectorElementType, DirectorScene, EntityRelation, Novel,
    NovelChapter, NovelVolume, RelationEvent, ShotDirecting, Track, TrackKind,
    STORY_ASSET_SCHEMAS, StoryAsset, StoryAssetType,
)

# 助手模型：保持原 shortplay/models.py 末尾的导入方式
from apps.creation.assistant.models import (  # noqa: E402,F401
    AgentCommunication, AgentRole, AssistantMemory, AssistantMessage, AssistantProposal,
    AssistantSession, CharacterVersion, CommunicationKind, ExperimentStatus, ScriptVersion,
    SkillLibrary, SkillScope, SkillType, StrategyExperiment,
)

__all__ = [
    # play
    "AuditMode", "Character", "CharacterRole", "ConsistencyMode", "Episode",
    "EpisodeStatus", "Gender", "Play", "PlayStatus", "RedoGranularity", "Scene",
    "SceneStatus", "Shot", "ShotStatus", "ShotType", "TimeOfDay", "VoiceProfile",
    # asset
    "Asset", "AssetDependency", "AssetLibrary", "AssetStatus", "AssetType",
    "RelationType",
    # agent
    "AgentConfig",
    # membership
    "AuditLog", "InvitationStatus", "MemberRole", "MemberStatus", "PlayInvitation",
    "PlayMember",
    # operations
    "ExecutionClaimStatus", "LocalTaskExecution", "ReviewDecision", "ReviewLog",
    "ReviewPoint", "TaskRecord", "TaskStatus", "TaskType",
    # novel（小说工作区，2026-11-23 组 1）
    "ChapterSnapshot", "EntityRelation", "Novel", "NovelChapter", "NovelVolume",
    "RelationEvent", "STORY_ASSET_SCHEMAS", "StoryAsset", "StoryAssetType",
    # canvas（无限画布，2026-11-23 组 4）
    "Canvas", "CanvasNode", "CanvasNodeType",
    # director（导演台 2D，2026-11-23 组 5）
    "DirectorElement", "DirectorElementType", "DirectorScene", "ShotDirecting",
    # timeline（多轨剪辑，2026-11-24 组 6）
    "Clip", "Composition", "Track", "TrackKind",
    # assistant
    "AgentCommunication", "AgentRole", "AssistantMemory", "AssistantMessage",
    "AssistantProposal", "AssistantSession", "CharacterVersion", "CommunicationKind",
    "ExperimentStatus", "ScriptVersion", "SkillLibrary", "SkillScope", "SkillType",
    "StrategyExperiment",
]
