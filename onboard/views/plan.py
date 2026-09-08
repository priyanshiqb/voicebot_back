from onboard.models.plan import Plan
from onboard.serializers.plan import (
    PlanSerializer,
    PlanListSerializer,
    PlanDropdownSerializer,
)
from utils.views.base_model_viewset import BaseViewSet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiParameter,
    OpenApiTypes,
    OpenApiResponse,
)
from onboard.serializers.onboarding import GeneralResponseSerializer


@extend_schema(tags=["Onboarding - Plans"])
@extend_schema_view(
    list=extend_schema(
        summary="List Plans",
        description="Get paginated list of plans with optional search, ordering, and filter attributes.",
        parameters=[
            OpenApiParameter(
                name="is_active",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="plan_type",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="currency",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name="search",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
            ),
        ],
        responses={
            200: PlanListSerializer(many=True),
        }
    ),
    create=extend_schema(
        summary="Create Plan",
        description="Create a new subscription plan.",
        responses={
            201: PlanSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required).")
        }
    ),
    retrieve=extend_schema(
        summary="Get Plan",
        description="Get specific plan details by ID.",
        responses={
            200: PlanSerializer,
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Plan not found.")
        }
    ),
    update=extend_schema(
        summary="Update Plan",
        description="Completely update an existing plan by ID.",
        responses={
            200: PlanSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Plan not found.")
        }
    ),
    partial_update=extend_schema(
        summary="Partial Update Plan",
        description="Partially update an existing plan by ID.",
        responses={
            200: PlanSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Plan not found.")
        }
    ),
    destroy=extend_schema(
        summary="Delete Plan",
        description="Soft-delete an existing plan by ID.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Plan deleted successfully."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied (superuser required)."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Plan not found.")
        }
    ),
    dropdown=extend_schema(
        summary="Get Dropdown Options",
        description="Get lightweight label/value selection pairs representing all active plans.",
        responses={
            200: PlanDropdownSerializer(many=True)
        }
    )
)
class PlanViewSet(BaseViewSet):
    """
    Plan ViewSet.
    Provides robust, production-grade endpoints for managing billing and subscription plans.
    """
    queryset = Plan.objects.all()

    def get_queryset(self):
        """Plans with their open rate rows attached.

        The serializer reports `is_minutes_based`, `is_fixed_quantity` and the entry
        price, and every one of those reads the plan's basis rate. Without the
        prefetch that is a query per plan on an endpoint that is public, unpaginated
        and hit by every visitor to the pricing page.
        """
        from django.db.models import Prefetch

        from wallet.models import CreditRate

        queryset = super().get_queryset().prefetch_related(
            Prefetch(
                "credit_rates",
                queryset=CreditRate.objects.filter(
                    is_deleted=False, effective_to__isnull=True
                ).order_by("operation"),
                to_attr="open_rates",
            )
        )
        if self.action in ["list", "dropdown"]:
            return queryset.filter(is_active=True)
        return queryset

    serializer_class = PlanSerializer
    list_serializer_class = PlanListSerializer
    dropdown_serializer_class = PlanDropdownSerializer

    search_fields = [
        "code",
        "name",
        "description",
    ]

    ordering_fields = [
        "name",
        "sort_order",
        "sort_order",
        "created_at",
    ]

    filterset_fields = {
        "code": ["exact"],
        "is_active": ["exact"],
        "plan_type": ["exact"],
        "currency": ["exact"],
        "billing_cycle": ["exact"],
        "tax_percentage": ["gte", "lte"],
        "discount_percentage": ["gte", "lte"],
        "created_at": ["date", "gte", "lte"],
    }

    # **Read-only. There is no write path here for anybody, superuser included.**
    #
    # This is the public pricing-page API -- `AllowAny` on read -- and it used to
    # accept writes from a superuser. That was a second door into the catalogue with
    # no audit trail and no completeness gate: a plan created through it was
    # instantly `is_active`, with zero entitlements and zero rates. A missing
    # allowance is not a limit of zero but no limit at all, and a missing rate cannot
    # price a call, so that plan was unlimited and unbillable at the same time --
    # silently, because neither failure raises anything.
    #
    # Blocked at the verb rather than the permission. A permission check is one
    # `get_permissions` override away from being loosened by someone who does not
    # know why it is there; `http_method_names` removes the routes, and
    # `bulk_delete` (a POST action inherited from BaseViewSet) goes with them.
    #
    # Catalogue writes live at `/api/v1/ops/plans/`, where they are gated on
    # `plan.change`, refused unless the plan is complete, and recorded.
    http_method_names = ["get", "head", "options"]

    def get_permissions(self):
        """Public read. Every action left on this viewset is one."""
        return [AllowAny()]

    @extend_schema(
        summary="Find the plan that sells a given number of minutes",
        description=(
            "The entry point for minutes-driven signup: the customer says how many "
            "minutes they need, and this returns the one plan that sells them, "
            "priced and with the credit grant worked out.\n\n"
            "**`selected_minutes` may differ from `requested_minutes`.** Where the "
            "catalogue is built from fixed-quantity packs, a request is rounded up "
            "to the smallest pack that covers it, and `rounded_up` says so. "
            "`selected_minutes` is the quantity that is actually sold, priced and "
            "granted — it is the number to send back when creating the order.\n\n"
            "Returns 404 when nothing covers the request, with the nearest bands "
            "either side so the page can say what *is* available."
        ),
        parameters=[
            OpenApiParameter(
                name="minutes",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Minutes the customer needs per billing cycle.",
            ),
            OpenApiParameter(
                name="billing_cycle",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="monthly (default) or yearly.",
            ),
            OpenApiParameter(
                name="currency",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="ISO code. INR by default.",
            ),
        ],
        responses={
            200: OpenApiResponse(description="The plan, its price and its credits."),
            400: OpenApiResponse(response=GeneralResponseSerializer,
                                 description="Missing or nonsensical quantity."),
            404: OpenApiResponse(response=GeneralResponseSerializer,
                                 description="No plan sells that quantity."),
        },
    )
    @action(detail=False, methods=["get"], url_path="for-minutes")
    def for_minutes(self, request):
        from onboard.services import plan_selection, pricing

        raw = request.query_params.get("minutes")
        try:
            minutes = int(raw)
        except (TypeError, ValueError):
            return self.error(
                message="Tell us how many minutes you need.",
                errors=["INVALID_MINUTES"],
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if minutes <= 0 or minutes > pricing.MAX_SELECTABLE_MINUTES:
            return self.error(
                message=(
                    f"Minutes must be between 1 and "
                    f"{pricing.MAX_SELECTABLE_MINUTES:,}."
                ),
                errors=["INVALID_MINUTES"],
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        billing_cycle = request.query_params.get("billing_cycle") or None
        currency = request.query_params.get("currency") or "INR"

        # Loaded once and passed down: `for_minutes` and `nearest_bands` would
        # otherwise each rebuild the whole ladder.
        ladder = plan_selection.ladder(billing_cycle=billing_cycle, currency=currency)
        plan, sold = plan_selection.for_minutes(minutes, plans=ladder)

        if plan is None:
            return self.error(
                message="We do not have a plan for that many minutes yet.",
                errors=["NO_PLAN_FOR_MINUTES"],
                data=plan_selection.nearest_bands(minutes, plans=ladder),
                status_code=status.HTTP_404_NOT_FOUND,
            )

        try:
            quote = pricing.quote_plan(plan, sold)
        except pricing.PricingError as exc:
            # The ladder said this plan sells the quantity and pricing disagreed,
            # which means the catalogue is inconsistent rather than the request bad.
            return self.error(
                message=str(exc),
                errors=["PLAN_NOT_SELLABLE"],
                status_code=status.HTTP_409_CONFLICT,
            )

        return self.success(
            data={
                "plan": {
                    "id": str(plan.pk),
                    "code": plan.code,
                    "name": plan.name,
                    "description": plan.description,
                    "billing_cycle": plan.billing_cycle,
                    "currency": plan.currency,
                    "min_minutes": plan.min_minutes,
                    "max_minutes": plan.max_minutes,
                    "is_fixed_quantity": plan.is_fixed_quantity,
                    "is_most_popular": plan.is_most_popular,
                },
                "requested_minutes": minutes,
                "selected_minutes": sold,
                # True only where a pack covers a smaller request. The page has to be
                # able to say "the 5,000-minute pack covers your 1,200" rather than
                # quietly quoting a number the customer never typed.
                "rounded_up": sold != minutes,
                "money": {
                    "basis": plan.credit_basis,
                    "price_per_minute_inr": str(quote.detail["price_per_minute_inr"]),
                    "gross": str(quote.detail["gross"]),
                    "discount": str(quote.detail["discount"]),
                    "net": str(quote.net),
                    "tax": str(quote.tax),
                    "total": str(quote.total),
                    "tax_percentage": str(quote.tax_percentage),
                    "currency": plan.currency,
                },
                "credits": {
                    "per_minute": quote.detail["credits_per_minute"],
                    "granted": quote.detail["allocated_credits"],
                    # What the grant is actually worth on each tier. Without this the
                    # promise "1,200 minutes" is only true on the basis tier, and the
                    # customer has no way to see that.
                    "equivalent_minutes": self._equivalent_minutes(
                        plan, quote.detail["allocated_credits"]
                    ),
                },
                "entitlements": PlanSerializer(plan).data["entitlements"],
                "rates": PlanSerializer(plan).data["rates"],
            },
            message="Plan matched",
        )

    def _equivalent_minutes(self, plan, credits):
        """How far the grant goes on each tier, not just the one it was sold at."""
        rates = getattr(plan, "open_rates", None)
        if rates is None:
            rates = plan.credit_rates.filter(is_deleted=False, effective_to__isnull=True)
        return {
            rate.operation: (credits // rate.credits_per_unit)
            for rate in rates
            if rate.credits_per_unit
        }
