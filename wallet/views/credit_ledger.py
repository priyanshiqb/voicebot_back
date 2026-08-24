import django_filters
from drf_spectacular.utils import extend_schema, extend_schema_view

from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from wallet.models.credit_ledger import CreditLedger
from wallet.serializers import CreditLedgerListSerializer, CreditLedgerSerializer


class CreditLedgerFilter(django_filters.FilterSet):
    """Date-range filters, because "what did I spend last month" is the question this
    endpoint exists to answer."""

    created_after = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_before = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="lte")
    is_credit = django_filters.BooleanFilter(method="filter_is_credit")

    class Meta:
        model = CreditLedger
        fields = ["entry_type", "bucket", "operation", "created_after", "created_before"]

    def filter_is_credit(self, queryset, name, value):
        return queryset.filter(amount__gt=0) if value else queryset.filter(amount__lt=0)


@extend_schema_view(
    list=extend_schema(
        tags=["Billing - Wallet"],
        summary="Credit ledger",
        description=(
            "Every credit movement for this tenant, newest first. Usage rows carry the "
            "rating that produced them, so each charge explains itself.\n\n"
            "Filter by `entry_type`, `bucket`, `operation`, `is_credit`, "
            "`created_after` / `created_before`."
        ),
    ),
    retrieve=extend_schema(tags=["Billing - Wallet"], summary="One ledger entry"),
)
class CreditLedgerViewSet(TenantBaseViewSet):
    """
    Read-only. The ledger is append-only, so there is nothing to expose beyond
    reading — `http_method_names` removes the write verbs at the router rather than
    relying on each one being individually overridden.

    Tenant scoping comes from `TenantBaseViewSet`: a tenant can only ever see rows
    where `tenant` is their own.
    """

    http_method_names = ["get", "head", "options"]

    queryset = CreditLedger.objects.all()
    serializer_class = CreditLedgerSerializer
    list_serializer_class = CreditLedgerListSerializer
    dropdown_serializer_class = None

    filterset_class = CreditLedgerFilter
    search_fields = ["reference_id", "notes", "operation"]
    ordering_fields = ["created_at", "amount", "balance_after"]
    ordering = ["-created_at"]

    select_related_fields = ["tenant", "wallet"]
    prefetch_related_fields = []
