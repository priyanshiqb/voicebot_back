from drf_spectacular.utils import extend_schema, extend_schema_view

from core.models.directory_contact import DirectoryContact
from core.serializers.directory_contact import (
    DirectoryContactDropdownSerializer,
    DirectoryContactListSerializer,
    DirectoryContactSerializer,
)
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet


@extend_schema(tags=["Company Directory"])
@extend_schema_view(
    list=extend_schema(
        summary="List directory contacts",
        description=(
            "The tenant's own people. Filter the tabs with `?category=EXECUTIVE` "
            "or `?category=OPERATIONS`; omit it for all. `?search=` matches name, "
            "designation, email and either phone column."
        ),
    ),
    retrieve=extend_schema(summary="Get one directory contact"),
    create=extend_schema(summary="Add a directory contact"),
    update=extend_schema(summary="Replace a directory contact"),
    partial_update=extend_schema(summary="Update a directory contact"),
    destroy=extend_schema(summary="Remove a directory contact (soft delete)"),
)
class DirectoryContactViewSet(TenantBaseViewSet):
    """
    The company directory — who works at this tenant.

    Not to be confused with `/core/contacts/`, which is people the tenant *calls*:
    leads and customers, with consent tracking and campaign membership. Nothing
    here is ever a campaign target.
    """

    queryset = DirectoryContact.objects.all()
    serializer_class = DirectoryContactSerializer
    list_serializer_class = DirectoryContactListSerializer
    dropdown_serializer_class = DirectoryContactDropdownSerializer

    # `category` is a stored, indexed column, so the tab filter is an index lookup
    # rather than a scan over job titles.
    filterset_fields = ["category", "department", "is_active"]

    # Both phone columns: a client searching "98765" is reading the number off the
    # screen — which is `phone_number`, as typed — while one searching "+9198…"
    # copied it from somewhere normalised.
    search_fields = ["name", "designation", "email", "phone_number", "phone_e164"]

    ordering_fields = ["name", "designation", "category", "created_at", "updated_at"]
    # Alphabetical: a directory is browsed, not monitored, so "who was added most
    # recently" is the wrong thing to put at the top.
    ordering = ["name"]
