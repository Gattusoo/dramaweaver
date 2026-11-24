"""公开接口路由（/api/public/）。"""
from django.urls import path

from . import views

urlpatterns = [
    path("plays/", views.PublicPlayListView.as_view(), name="public-play-list"),
    path("plays/<int:pk>/", views.PublicPlayDetailView.as_view(), name="public-play-detail"),
    path("stats/", views.PublicStatsView.as_view(), name="public-stats"),
]
