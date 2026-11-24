"""项目顶层 URL 路由。

URL 路径与旧版完全一致：/api/auth/、/api/ai-gateway/、/api/analytics/，
以及 router 注册的资源前缀（plays/episodes/.../publications 等）。
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.ai_gateway import views as ai_gateway_views
from apps.billing import views as billing_views
from apps.creation import views as creation_views
from apps.creation.assistant import views as assistant_views
from apps.distribution import analytics_views as distribution_analytics
from apps.distribution import views as distribution_views
from apps.public_api import urls as public_api_urls

# ─── 认证 ────────────────────────────────────────────────────────────────────
from apps.core.system.views import auth as auth_views

auth_patterns = [
    path("auth/register/", auth_views.RegisterView.as_view(), name="auth-register"),
    path("auth/login/", auth_views.LoginView.as_view(), name="auth-login"),
    path("auth/refresh/", auth_views.RefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", auth_views.LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", auth_views.MeView.as_view(), name="auth-me"),
    path("auth/users/<int:pk>/platform-admin/", auth_views.PlatformAdminFlagView.as_view(), name="auth-platform-admin"),
]

# ─── AI Gateway 函数端点 ─────────────────────────────────────────────────────
ai_gateway_patterns = [
    # 创作者可读的创作模型列表（轻量视图，非 admin）；必须先于 router 的 ai-models detail 路由匹配
    path("ai-models/creation/", ai_gateway_views.creation_models, name="ai-model-creation"),
    path("ai-gateway/status/", ai_gateway_views.ai_gateway_status, name="ai-gateway-status"),
    path("ai-gateway/adapters/", ai_gateway_views.supported_adapters, name="ai-gateway-adapters"),
    path("ai-gateway/providers/<int:pk>/test/", ai_gateway_views.provider_test, name="ai-gateway-provider-test"),
    path("ai-gateway/providers/<int:pk>/models/", ai_gateway_views.provider_models, name="ai-gateway-provider-models"),
    path("ai-gateway/providers/<int:pk>/models/import/", ai_gateway_views.import_provider_models, name="ai-gateway-provider-models-import"),
]

# ─── 分析 ────────────────────────────────────────────────────────────────────
analytics_patterns = [
    path("analytics/plays/<int:pk>/cost/", distribution_analytics.play_cost, name="analytics-play-cost"),
    path("analytics/cost/by-provider/", distribution_analytics.cost_provider, name="analytics-cost-provider"),
    path("analytics/cost/by-model/", distribution_analytics.cost_model, name="analytics-cost-model"),
    path("analytics/cost-optimization/report/", distribution_analytics.cost_optimization_report_view,
         name="analytics-cost-optimization-report"),
    path("analytics/production/stats/", distribution_analytics.production_stats_view, name="analytics-production-stats"),
    path("analytics/publications/summary/", distribution_analytics.publication_summary_view, name="analytics-publication-summary"),
]

# ─── 资源 router ─────────────────────────────────────────────────────────────
router = DefaultRouter()
# creation
router.register(r"plays", creation_views.PlayViewSet, basename="play")
router.register(r"episodes", creation_views.EpisodeViewSet)
router.register(r"scenes", creation_views.SceneViewSet)
router.register(r"shots", creation_views.ShotViewSet)
router.register(r"characters", creation_views.CharacterViewSet)
router.register(r"voice-profiles", creation_views.VoiceProfileViewSet)
router.register(r"assets", creation_views.AssetViewSet)
router.register(r"asset-dependencies", creation_views.AssetDependencyViewSet)
router.register(r"asset-library", creation_views.AssetLibraryViewSet, basename="asset-library")
router.register(r"agent-configs", creation_views.AgentConfigViewSet)
router.register(r"task-records", creation_views.TaskRecordViewSet)
router.register(r"review-logs", creation_views.ReviewLogViewSet)

# 小说工作区（2026-11-23 组 1）
router.register(r"novels", creation_views.NovelViewSet)
router.register(r"novel-volumes", creation_views.NovelVolumeViewSet)
router.register(r"novel-chapters", creation_views.NovelChapterViewSet)
router.register(r"chapter-snapshots", creation_views.ChapterSnapshotViewSet)
router.register(r"story-assets", creation_views.StoryAssetViewSet)
router.register(r"entity-relations", creation_views.EntityRelationViewSet)
# 无限画布（2026-11-23 组 4）
router.register(r"canvases", creation_views.CanvasViewSet)
router.register(r"canvas-nodes", creation_views.CanvasNodeViewSet)
# 导演台 2D（2026-11-23 组 5）
router.register(r"director-scenes", creation_views.DirectorSceneViewSet)
router.register(r"director-elements", creation_views.DirectorElementViewSet)
router.register(r"shot-directing", creation_views.ShotDirectingViewSet)
# 多轨剪辑（2026-11-24 组 6）
router.register(r"compositions", creation_views.CompositionViewSet)
router.register(r"tracks", creation_views.TrackViewSet)
router.register(r"clips", creation_views.ClipViewSet)
# ai_gateway
router.register(r"ai-providers", ai_gateway_views.AIProviderViewSet, basename="ai-provider")
router.register(r"ai-models", ai_gateway_views.AIModelViewSet)
router.register(r"cost-records", ai_gateway_views.CostRecordViewSet)
router.register(r"user-credentials", ai_gateway_views.UserProviderCredentialViewSet,
                basename="user-credential")
# distribution
router.register(r"subtitle-tracks", distribution_views.SubtitleTrackViewSet)
router.register(r"distribution-channels", distribution_views.DistributionChannelViewSet)
router.register(r"publications", distribution_views.PublicationViewSet)
# assistant
router.register(r"assistant/sessions", assistant_views.AssistantSessionViewSet, basename="assistant-session")
router.register(r"assistant/proposals", assistant_views.AssistantProposalViewSet, basename="assistant-proposal")
router.register(r"assistant/memories", assistant_views.AssistantMemoryViewSet, basename="assistant-memory")
router.register(r"assistant/skills", assistant_views.AssistantSkillViewSet, basename="assistant-skill")
router.register(r"assistant/evaluation", assistant_views.AssistantEvaluationViewSet, basename="assistant-evaluation")
router.register(r"assistant/cost", assistant_views.AssistantCostEstimateViewSet, basename="assistant-cost")
router.register(r"assistant/review", assistant_views.AssistantReviewViewSet, basename="assistant-review")
router.register(r"assistant/review/feedback", assistant_views.AssistantReviewFeedbackViewSet,
                basename="assistant-review-feedback")
router.register(r"inspirations", assistant_views.InspirationViewSet, basename="inspiration")
router.register(r"assistant/skill-library", assistant_views.AssistantSkillLibraryViewSet,
                basename="assistant-skill-library")
router.register(r"assistant/experiments", assistant_views.AssistantExperimentViewSet,
                basename="assistant-experiment")
router.register(r"assistant/tools", assistant_views.AssistantToolViewSet,
                basename="assistant-tool")
router.register(r"assistant/trust", assistant_views.AssistantTrustViewSet,
                basename="assistant-trust")

# 剧本版本 / 角色版本（Phase 2.4.3）：按短剧/角色嵌套，用显式路径而非 router
version_patterns = [
    path("api/scripts/<int:play_id>/versions/merge/",
         assistant_views.ScriptVersionViewSet.as_view({"post": "merge"}), name="script-version-merge"),
    path("api/scripts/<int:play_id>/versions/",
         assistant_views.ScriptVersionViewSet.as_view({"get": "list", "post": "create"}),
         name="script-version-list"),
    path("api/scripts/<int:play_id>/versions/<int:pk>/branch/",
         assistant_views.ScriptVersionViewSet.as_view({"post": "branch"}), name="script-version-branch"),
    path("api/scripts/<int:play_id>/versions/<int:pk>/diff/",
         assistant_views.ScriptVersionViewSet.as_view({"get": "diff"}), name="script-version-diff"),
    path("api/characters/<int:character_id>/versions/",
         assistant_views.CharacterVersionViewSet.as_view({"get": "list", "post": "create"}),
         name="character-version-list"),
]

# ─── 公开区（匿名只读，不鉴权）──────────────────────────────────────────────
public_patterns = [
    path("public/", include(public_api_urls)),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include(auth_patterns)),
    path("api/", include(ai_gateway_patterns)),
    path("api/", include(analytics_patterns)),
    path("api/", include(public_patterns)),
    # 平台管理员审核通道（只读快照 + 冻结/解冻，强制审计日志；独立于业务 ViewSet）
    path("api/admin/plays/<int:pk>/review/", creation_views.PlayReviewView.as_view(), name="admin-play-review"),
    path("api/admin/plays/<int:pk>/freeze/", creation_views.PlayFreezeView.as_view(), name="admin-play-freeze"),
    path("api/admin/plays/<int:pk>/unfreeze/", creation_views.PlayUnfreezeView.as_view(), name="admin-play-unfreeze"),
    # 计费：管理员钱包通道 + 用户自查
    path("api/admin/wallets/", billing_views.AdminWalletListView.as_view(), name="admin-wallet-list"),
    path("api/admin/wallets/<int:user_pk>/adjust/", billing_views.AdminWalletAdjustView.as_view(), name="admin-wallet-adjust"),
    path("api/wallet/", billing_views.MyWalletView.as_view(), name="my-wallet"),
    *version_patterns,
    # 实时通道降级判据（主计划 3.2.12）：认证只读；路径独立于 ws 路由，
    # 不受 ENABLE_REALTIME_EVENTS 限制（关闭时恰是前端要探测的场景）
    path("api/assistant/realtime/config/",
         assistant_views.AssistantRealtimeConfigViewSet.as_view({"get": "list"}),
         name="assistant-realtime-config"),
    path("api/", include(router.urls)),
    # 运行健康（工作台 Stage C2）：认证用户只读，脱敏（无路径无密钥）
    path("api/health/", creation_views.system_health, name="system-health"),
]

# 开发环境由 Django 直接服务本地媒体（本地存储产物存于 MEDIA_ROOT，返回 /media/... 相对地址）。
# 生产须由 nginx / 对象存储直接服务 /media/ 与 STATIC_URL，不经过 Django。
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
