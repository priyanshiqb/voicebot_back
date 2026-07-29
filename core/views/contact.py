import django_filters
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser

from core.models.contact import Contact
from core.resources.contact import ContactResource
from core.serializers.contact import (
    ContactDropdownSerializer,
    ContactListSerializer,
    ContactLookupQuerySerializer,
    ContactSerializer,
)
from core.views.mixins import ImportExportMixin
from onboard.serializers.onboarding import GeneralResponseSerializer
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet


class ContactFilter(django_filters.FilterSet):
    """
    ArrayField needs explicit filters — django-filter cannot auto-generate them and
    raises at filterset build time if `tags` is left in filterset_fields.
    """

    tags = django_filters.CharFilter(
        method="filter_tags_any",
        help_text="Comma-separated. Matches contacts carrying ANY of these tags.",
    )
    tags_all = django_filters.CharFilter(
        method="filter_tags_all",
        help_text="Comma-separated. Matches contacts carrying ALL of these tags.",
    )

    class Meta:
        model = Contact
        fields = ["is_active", "do_not_call", "consent_status", "source", "company"]

    @staticmethod
    def _split(value):
        return [item.strip() for item in (value or "").split(",") if item.strip()]

    def filter_tags_any(self, queryset, name, value):
        tags = self._split(value)
        return queryset.filter(tags__overlap=tags) if tags else queryset

    def filter_tags_all(self, queryset, name, value):
        tags = self._split(value)
        return queryset.filter(tags__contains=tags) if tags else queryset


@extend_schema(tags=["Contacts"])
@extend_schema_view(
    list=extend_schema(
        summary="List Contacts",
        description="Paginated, tenant-scoped contact list with search and filtering.",
    ),
    retrieve=extend_schema(summary="Get Contact", description="Get a contact by ID."),
    create=extend_schema(
        summary="Create Contact",
        description="Create a contact. The phone number is normalized to E.164 on save.",
    ),
    update=extend_schema(summary="Update Contact"),
    partial_update=extend_schema(summary="Partially Update Contact"),
    destroy=extend_schema(
        summary="Delete Contact",
        description="Soft-delete a contact. This frees its phone number for reuse.",
    ),
)
class ContactViewSet(ImportExportMixin, TenantBaseViewSet):
    """
    Contact Management API.

    Bulk creation is deliberately not exposed here — it goes through the staged
    contact-import flow, which adds conflict review before anything is written.
    """

    queryset = Contact.objects.all()
    serializer_class = ContactSerializer
    list_serializer_class = ContactListSerializer
    dropdown_serializer_class = ContactDropdownSerializer
    resource_class = ContactResource

    filterset_class = ContactFilter
    search_fields = [
        "first_name",
        "last_name",
        "phone_e164",
        "phone_number",
        "email_address",
        "company",
    ]
    ordering_fields = ["created_at", "updated_at", "first_name", "last_name", "company"]
    ordering = ["-created_at"]
    select_related_fields = ["tenant"]

    @extend_schema(exclude=True)
    @action(
        detail=False,
        methods=["post"],
        url_path="import",
        parser_classes=[MultiPartParser, FormParser],
    )
    def import_data(self, request, *args, **kwargs):
        """
        Disabled. Direct import would bypass phone normalization and conflict review;
        use POST /api/v1/core/contact-imports/ instead.

        Re-declared as an action deliberately: overriding the mixin's method without
        the decorator would drop the route entirely rather than return a clear error.
        """
        return self.error(
            message=(
                "Direct import is not supported for contacts. "
                "Use the staged import flow at /api/v1/core/contact-imports/."
            ),
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    @extend_schema(
        summary="Look up a contact by phone number",
        description=(
            "Normalizes the supplied number to E.164 and returns the matching contact. "
            "Intended for inbound-caller resolution by the voice runtime."
        ),
        parameters=[
            OpenApiParameter(
                name="phone",
                description="Phone number in any format, e.g. +14155552671.",
                required=True,
                type=str,
            ),
            OpenApiParameter(
                name="region",
                description="ISO-3166 alpha-2 region used to parse non-international numbers.",
                required=False,
                type=str,
            ),
        ],
        responses={
            200: ContactSerializer,
            400: OpenApiResponse(
                response=GeneralResponseSerializer,
                description="Missing or unparseable phone number.",
            ),
            404: OpenApiResponse(
                response=GeneralResponseSerializer, description="No matching contact."
            ),
        },
    )
    @action(detail=False, methods=["get"], url_path="lookup")
    def lookup(self, request, *args, **kwargs):
        query = ContactLookupQuerySerializer(data=request.query_params)
        if not query.is_valid():
            first_error = next(iter(query.errors.values()))[0]
            return self.error(
                message=str(first_error), status_code=status.HTTP_400_BAD_REQUEST
            )

        contact = (
            self.get_queryset()
            .filter(phone_e164=query.validated_data["phone_e164"])
            .first()
        )
        if contact is None:
            return self.error(
                message="No contact found for that phone number.",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        return self.success(
            data=ContactSerializer(contact, context={"request": request}).data,
            message="Retrieved successfully",
        )

    @extend_schema(
        summary="List tags in use",
        description="Distinct tags across this tenant's contacts, with usage counts.",
        responses={200: OpenApiResponse(response=GeneralResponseSerializer)},
    )
    @action(detail=False, methods=["get"], url_path="tags")
    def tags(self, request, *args, **kwargs):
        rows = (
            self.get_queryset()
            .exclude(tags=[])
            .values_list("tags", flat=True)
        )

        counts = {}
        for tag_list in rows:
            for tag in tag_list or []:
                counts[tag] = counts.get(tag, 0) + 1

        data = sorted(
            ({"tag": tag, "count": count} for tag, count in counts.items()),
            key=lambda item: (-item["count"], item["tag"].lower()),
        )
        return self.success(data=data, message="Retrieved successfully")
