from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

class GeographyDropdownPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 25

    def get_paginated_response(self, data):
        return Response({
            "status": True,
            "message": "success",
            "data": data,
            "errors": None,
            "meta": {
                "count": self.page.paginator.count,
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "page": int(self.request.query_params.get("page", 1)),
                "page_size": int(self.request.query_params.get(self.page_size_query_param, self.page_size)),
            }
        })
