"""统一分页器（design D4 / platform/api-conventions）。

参数：page（从 1 开始）、limit（默认 20，最大 200）。
越界页返回空列表（不报 404），且 total 仍为总数。get_paginated_response 返回
{page, limit, total, results}，由渲染器提升为统一结构。
"""
from django.core.paginator import InvalidPage
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class CustomPagination(PageNumberPagination):
    page_query_param = "page"
    page_size_query_param = "limit"
    page_size = 20
    max_page_size = 200

    def paginate_queryset(self, queryset, request, view=None):
        self.request = request
        page_size = self.get_page_size(request)
        if not page_size:
            return None
        paginator = self.django_paginator_class(queryset, page_size)
        page_number = self.get_page_number(request, paginator)
        try:
            self.page = paginator.page(page_number)
        except InvalidPage:
            # 页码越界或非法：返回空列表，但保留 total 供响应体输出
            self.page = None
            self._oob_total = paginator.count
            try:
                self._oob_page = int(page_number)
            except (TypeError, ValueError):
                self._oob_page = 1
            self._oob_limit = page_size
            return []
        return list(self.page)

    def get_paginated_response(self, data):
        if self.page is not None:
            page = self.page.number
            limit = self.page.paginator.per_page
            total = self.page.paginator.count
        else:
            page = getattr(self, "_oob_page", 1)
            limit = getattr(self, "_oob_limit", self.page_size)
            total = getattr(self, "_oob_total", 0)
        return Response({
            "page": page,
            "limit": limit,
            "total": total,
            "results": data,
        })

