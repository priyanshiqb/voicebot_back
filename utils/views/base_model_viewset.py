from rest_framework.viewsets import ModelViewSet
from rest_framework.permissions import IsAuthenticated
from rest_framework import filters, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from django.db import transaction
from django_filters.rest_framework import DjangoFilterBackend
from user.models import CustomUser
from django.db import models
from drf_spectacular.utils import (
    extend_schema,
    OpenApiExample,
)
from utils.permissions.login_window import (
    IsWithinLoginWindow
)

class SetPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100

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
                "page_size": int(
                    self.request.query_params.get(
                        "page_size", 
                        self.page_size
                    )
                ),
            }
        })

class ResponseMixin:

    def success(self, data=None, message="success", status_code=200):
        return Response({
            "status": True,
            "message": message,
            "data": data,
            "errors": None
        }, status=status_code)

    def error(self, message="error", errors=None, status_code=400, data=None):
        return Response({
            "status": False,
            "message": message,
            "data": data,
            "errors": errors
        }, status=status_code)



class BaseViewSet(ResponseMixin, ModelViewSet):
    permission_classes = (IsAuthenticated,)
    pagination_class = SetPagination

    queryset = None # type: models.QuerySet
    serializer_class = None
    list_serializer_class = None
    dropdown_serializer_class = None

    """
        DRF Search & Ordering (GET query params)

        - Search:
        ?search=iphone
        ?search=iphone%20black / iphone+black
        → OR across `search_fields`, multiple terms act as AND.
        → Supports: = exact, ^ startswith, @ full-text (Postgres).

        - Ordering:
        ?ordering=price
        ?ordering=-price
        ?ordering=price,-rating
        → ASC by default, "-" for DESC, supports multiple fields.

        - Notes:
        - Only configured `search_fields` / `ordering_fields` are used
        - Works with pagination automatically
    """
    filter_backends = (
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    )

    """
        ======================== DJANGO FILTER LOOKUPS ========================

        Use with:
        ?field__lookup=value

        Example:
        ?price__gte=100&price__lte=500
        ?name__icontains=iphone
        ?category__in=1,2,3


        -----------------------------------------------------------------------
        1. NUMERIC / COMPARISON (IntegerField, FloatField, DateField)
        -----------------------------------------------------------------------
        ["exact", "gt", "gte", "lt", "lte", "range"]

        exact      → exact match
        ?price=100

        gt         → greater than
        ?price__gt=100

        gte        → greater than or equal
        ?price__gte=100

        lt         → less than
        ?price__lt=500

        lte        → less than or equal
        ?price__lte=500

        range      → between two values (inclusive)
        ?price__range=100,500


        -----------------------------------------------------------------------
        2. TEXT LOOKUPS (CharField, TextField)
        -----------------------------------------------------------------------
        ["exact", "iexact", "contains", "icontains",
        "startswith", "istartswith", "endswith", "iendswith",
        "regex", "iregex"]

        exact         → exact match (case-sensitive)
        ?name=iphone

        iexact        → exact match (case-insensitive)
        ?name__iexact=iphone

        contains      → contains substring (case-sensitive)
        ?name__contains=phone

        icontains     → contains substring (case-insensitive, most used)
        ?name__icontains=phone

        startswith    → starts with (case-sensitive)
        ?name__startswith=iph

        istartswith   → starts with (case-insensitive)
        ?name__istartswith=iph

        endswith      → ends with (case-sensitive)
        ?name__endswith=pro

        iendswith     → ends with (case-insensitive)
        ?name__iendswith=pro

        regex         → matches regex pattern (case-sensitive)
        ?name__regex=^iph.*pro$

        iregex        → matches regex pattern (case-insensitive)
        ?name__iregex=^iph.*pro$


        -----------------------------------------------------------------------
        3. IN LOOKUP (MULTIPLE VALUES)
        -----------------------------------------------------------------------
        ["in"]

        in        → matches any value in list (comma-separated)
        ?category__in=1,2,3
        ?status__in=active,inactive


        -----------------------------------------------------------------------
        4. NULL CHECK
        -----------------------------------------------------------------------
        ["isnull"]

        isnull    → checks if value is NULL or NOT NULL
        ?deleted_at__isnull=true
        ?deleted_at__isnull=false


        -----------------------------------------------------------------------
        5. DATE / TIME LOOKUPS (DateField, DateTimeField)
        -----------------------------------------------------------------------
        ["date", "year", "month", "day", "week", "week_day",
        "quarter", "hour", "minute", "second"]

        date        → exact date match
        ?created_at__date=2025-04-01

        year        → filter by year
        ?created_at__year=2025

        month       → filter by month (1–12)
        ?created_at__month=4

        day         → filter by day (1–31)
        ?created_at__day=10

        week        → filter by week number
        ?created_at__week=15

        week_day    → day of week (1=Sunday, 7=Saturday)
        ?created_at__week_day=1

        quarter     → quarter of year (1–4)
        ?created_at__quarter=2

        hour        → hour of day (0–23)
        ?created_at__hour=14

        minute      → minute value
        ?created_at__minute=30

        second      → second value
        ?created_at__second=45


        -----------------------------------------------------------------------
        6. RANGE LOOKUP
        -----------------------------------------------------------------------
        ["range"]

        range    → between two values (inclusive, shorthand for gte + lte)
        ?price__range=100,500


        -----------------------------------------------------------------------
        7. BOOLEAN
        -----------------------------------------------------------------------
        ["exact"]

        exact    → true/false match
        ?is_active=true
        ?is_active=false


        -----------------------------------------------------------------------
        8. RELATIONSHIP LOOKUPS (ForeignKey / Nested)
        -----------------------------------------------------------------------

        → filter using related model fields

        ?category=1
        ?category__name=electronics
        ?category__name__icontains=mobile


        -----------------------------------------------------------------------
        9. POSTGRES ONLY (ArrayField / JSONField)
        -----------------------------------------------------------------------
        ["contains", "contained_by", "overlap"]

        contains        → field contains given value
        ?tags__contains=mobile

        contained_by    → field is subset of given values
        ?tags__contained_by=mobile,electronics

        overlap         → any overlap between field and values
        ?tags__overlap=mobile,electronics


        -----------------------------------------------------------------------
        NOTES:
        -----------------------------------------------------------------------
        - Combine filters:
        ?price__gte=100&price__lte=500&category=1

        - Use icontains for search-like filtering

        - "in" values must be comma-separated (no brackets)

        - Prefer gte/lte over range for better validation

        - Avoid exposing "__all__" for security & performance reasons

        =======================================================================
    """
    filterset_fields = []

    """
        ======================== DRF SEARCH FILTER ========================

        Use with:
        ?search=value

        Example:
        ?search=iphone
        ?search=iphone black


        -----------------------------------------------------------------------
        HOW IT WORKS
        -----------------------------------------------------------------------

        - Applies search on fields defined in `search_fields`
        - Uses partial matching (LIKE / ILIKE)
        - Case-insensitive by default
        - Splits multiple words by space


        -----------------------------------------------------------------------
        1. BASIC SEARCH
        -----------------------------------------------------------------------

        search_fields = ["name", "description"]

        ?search=iphone

        → Matches records where:
        name contains "iphone"
        OR description contains "iphone"

        (SQL equivalent)
        WHERE name ILIKE '%iphone%'
        OR description ILIKE '%iphone%'


        -----------------------------------------------------------------------
        2. MULTIPLE WORD SEARCH
        -----------------------------------------------------------------------

        ?search=iphone black

        → Each word is searched separately

        (SQL equivalent)
        (name ILIKE '%iphone%' OR description ILIKE '%iphone%')
        AND
        (name ILIKE '%black%' OR description ILIKE '%black%')

        → Words = AND
        → Fields = OR


        -----------------------------------------------------------------------
        3. SEARCH FIELD PREFIXES
        -----------------------------------------------------------------------

        You can control behavior using prefixes in `search_fields`:

        "field"     → default contains (ILIKE %value%)
        "name"

        "=field"    → exact match
        "=name"

        "^field"    → starts with
        "^name"

        "@field"    → full-text search (PostgreSQL only)
        "@description"


        -----------------------------------------------------------------------
        4. EXAMPLES
        -----------------------------------------------------------------------

        search_fields = ["name"]
        ?search=iph
        → name ILIKE '%iph%'

        search_fields = ["^name"]
        ?search=iph
        → name LIKE 'iph%'

        search_fields = ["=name"]
        ?search=iphone
        → name = 'iphone'


        -----------------------------------------------------------------------
        5. RELATIONSHIP SEARCH
        -----------------------------------------------------------------------

        You can search related fields:

        search_fields = ["category__name"]

        ?search=mobile
        → category.name ILIKE '%mobile%'


        -----------------------------------------------------------------------
        6. IMPORTANT NOTES
        -----------------------------------------------------------------------

        - Search is slower than exact filters (uses LIKE)
        - Avoid too many fields in search_fields (performance impact)
        - Use `icontains` behavior by default (case-insensitive)
        - Works together with filterset_fields (AND logic outside search)

        Example:
        ?search=iphone&category=1

        → (search condition)
        AND
        (category = 1)


        =======================================================================
            """
    # add db_index=True to frequently searched fields for better performance
    search_fields = []

    """
        ======================== DRF ORDERING FILTER ========================

        Use with:
        ?ordering=field

        Example:
        ?ordering=price
        ?ordering=-price
        ?ordering=price,-rating


        -----------------------------------------------------------------------
        HOW IT WORKS
        -----------------------------------------------------------------------

        - Sorts the queryset based on fields defined in `ordering_fields`
        - Ascending order by default
        - Prefix "-" for descending order
        - Supports multiple fields


        -----------------------------------------------------------------------
        1. BASIC ORDERING
        -----------------------------------------------------------------------

        ordering_fields = ["price"]

        ?ordering=price

        → Sorts by price (ascending)

        (SQL equivalent)
        ORDER BY price ASC


        -----------------------------------------------------------------------
        2. DESCENDING ORDER
        -----------------------------------------------------------------------

        ?ordering=-price

        → Sorts by price (descending)

        (SQL equivalent)
        ORDER BY price DESC


        -----------------------------------------------------------------------
        3. MULTIPLE FIELD ORDERING
        -----------------------------------------------------------------------

        ordering_fields = ["price", "rating"]

        ?ordering=price,-rating

        → First sort by price ASC
        → Then sort by rating DESC

        (SQL equivalent)
        ORDER BY price ASC, rating DESC


        -----------------------------------------------------------------------
        4. DEFAULT ORDERING
        -----------------------------------------------------------------------

        ordering = ["-created_at"]

        → Applied when no ?ordering is provided

        (SQL equivalent)
        ORDER BY created_at DESC


        -----------------------------------------------------------------------
        5. RELATIONSHIP ORDERING
        -----------------------------------------------------------------------

        You can order using related fields:

        ordering_fields = ["category__name"]

        ?ordering=category__name
        ?ordering=-category__name

        → Sorts by related model field


        -----------------------------------------------------------------------
        6. IMPORTANT NOTES
        -----------------------------------------------------------------------

        - Only fields in `ordering_fields` are allowed
        - Prevents unsafe or unintended sorting
        - Use "__all__" to allow all fields (NOT recommended)

        Example:
        ordering_fields = "__all__"   ❌ (use carefully)

        - Always define safe fields for production


        -----------------------------------------------------------------------
        7. COMBINING WITH FILTERS & SEARCH
        -----------------------------------------------------------------------

        Example:
        ?search=iphone&category=1&ordering=-price

        → Apply search filter
        → Apply field filters
        → Then apply ordering


        =======================================================================
    """
    ordering_fields = ["created_at", "updated_at"]
    ordering = ["-created_at"]

    select_related_fields = []
    prefetch_related_fields = []

    def get_queryset(self):
        """
        for future use
        def get_queryset(self):
            queryset = super().get_queryset()
            if hasattr(self, "select_related_fields"):
                queryset = queryset.select_related(*self.select_related_fields)
            if hasattr(self, "prefetch_related_fields"):
                queryset = queryset.prefetch_related(*self.prefetch_related_fields)
            return queryset
        """
        assert self.queryset is not None, "queryset must be defined"

        queryset = self.queryset.all()

        if hasattr(self, "select_related_fields") and self.select_related_fields:
            queryset = queryset.select_related(*self.select_related_fields)
        if hasattr(self, "prefetch_related_fields") and self.prefetch_related_fields:
            queryset = queryset.prefetch_related(*self.prefetch_related_fields)

        if hasattr(queryset.model, "is_deleted"):
            return queryset.filter(is_deleted=False)

        return queryset

    def get_serializer_class(self):
        if not self.serializer_class:
            raise NotImplementedError("serializer_class must be defined")

        if self.action == "list" and self.list_serializer_class:
            return self.list_serializer_class

        if self.action == "dropdown" and self.dropdown_serializer_class:
            return self.dropdown_serializer_class

        return self.serializer_class

    def perform_create(self, serializer):
        model = serializer.Meta.model

        data = {}

        if hasattr(model, "created_by"):
            data["created_by"] = self.request.user

        if hasattr(model, "updated_by"):
            data["updated_by"] = self.request.user

        serializer.save(**data)

    def perform_update(self, serializer):
        model = serializer.Meta.model

        data = {}

        if hasattr(model, "updated_by"):
            data["updated_by"] = self.request.user

        serializer.save(**data)

    def perform_destroy(self, instance):
        if hasattr(instance, "soft_delete"):
            instance.soft_delete(user=self.request.user)
        else:
            instance.delete()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        return self.success(
            data=serializer.data, 
            message="Created successfully", 
            status_code=status.HTTP_201_CREATED
        )
    
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return self.success(data=serializer.data, message="Retrieved successfully")
    
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        return self.success(data=serializer.data, message="Updated successfully")
    
    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self.perform_destroy(instance)
        return self.success(message="Deleted successfully")
    
    
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(queryset)

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
            

        serializer = self.get_serializer(queryset, many=True)
        return self.success(data=serializer.data, message="Listed successfully")

    @extend_schema(
        parameters=[],
        responses={200: None},
    )
    @action(detail=False, methods=["get"])
    def dropdown(self, request):
        queryset = self.filter_queryset(self.get_queryset())

        max_limit = 1000
        limit = request.query_params.get("limit", None)
        if limit and limit.isdigit():
            limit = int(limit)
            if limit > max_limit:
                return self.error(
                    message=f"Limit cannot exceed {max_limit}",
                    status_code=status.HTTP_400_BAD_REQUEST
                )
            queryset = queryset[:limit]

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        queryset = queryset[:max_limit]
        serializer = self.get_serializer(queryset, many=True)
        return self.success(data=serializer.data, message="Dropdown options retrieved successfully")

    @extend_schema(
        request={
            "application/json": {
                "example": {
                    "ids": [
                        "uuid-1",
                        "uuid-2"
                    ]
                }
            }
        },
        responses={200: None},
    )
    @action(detail=False, methods=["post"])
    def bulk_delete(self, request):
        ids = request.data.get("ids", [])

        if not isinstance(ids, list) or not ids:
            return self.error(message="ids must be a non-empty list", status_code=status.HTTP_400_BAD_REQUEST)

        model = self.get_queryset().model
        if hasattr(model, "is_deleted"):
            queryset = self.get_queryset().filter(id__in=ids, is_deleted=False)
        else:
            queryset = self.get_queryset().filter(id__in=ids)
        if queryset.count() != len(ids):
            return self.error(message="Some ids are invalid", status_code=status.HTTP_400_BAD_REQUEST)
            
        with transaction.atomic():
            if hasattr(queryset.model, "is_deleted"):
                for obj in queryset:
                    obj.soft_delete(user=request.user)
            else:
                queryset.delete()

        return self.success(message="Bulk deleted successfully")








