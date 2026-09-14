"""Plan configuration endpoints.

A thin layer. Everything with a rule in it lives in ``ops.services.plan`` — these
views validate, call a service, write an audit row and shape a response. That split is
what lets the same completeness gate serve the activate endpoint, the list's
``7/7`` column and the console's review screen without three implementations.

**Not built on the tenant-scoped viewsets.** Those resolve the tenant from
``request.user.tenant_mapping``; a platform operator has none, and mounting an
operator screen on one silently scopes it to the operator's own tenant.
"""

import logging

from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.viewsets import GenericViewSet

from onboard.models import Plan
from ops.permissions import HasConsolePermission
from ops.serializers.plan import (
    CreditRateCardSerializer,
    PlanCloneSerializer,
    PlanConfigurationSerializer,
    PlanEntitlementGridSerializer,
    PlanFieldsSerializer,
    PlanListSerializer,
)
from ops.services.plan import clone as clone_service
from ops.services.plan import config, lifecycle, readiness
from user.models import OperatorAuditLog
from user.services import operator_audit
from utils.views.base_model_viewset import ResponseMixin, SetPagination
from utils.views.ordering import MappedOrderingFilter

logger = logging.getLogger(__name__)

Action = OperatorAuditLog.Action


@extend_schema(tags=["Ops - Plans"])
class PlanConfigurationViewSet(ResponseMixin, GenericViewSet):
    """The plan catalogue, as commercial edits it.

    Every write requires ``plan.change`` and leaves an audit row. Reads require only
    platform staff — a console where support cannot look at a plan is not a console.
    """

    permission_classes = (HasConsolePermission,)
    serializer_class = PlanConfigurationSerializer
    pagination_class = SetPagination

    # Spelled out only to swap the stock OrderingFilter for MappedOrderingFilter,
    # which is what honours `ordering_tiebreaker`. A GenericViewSet would otherwise
    # pick the same three backends up from DEFAULT_FILTER_BACKENDS.
    filter_backends = (DjangoFilterBackend, filters.SearchFilter, MappedOrderingFilter)
    # Plain columns with choices, so django-filter builds a strict ChoiceFilter for
    # each: an unknown or wrong-case value is a 400 naming the field, not an
    # unfiltered list that looks like a real answer.
    filterset_fields = ["status", "billing_cycle"]
    search_fields = ["name", "code"]
    # `subscriber_count` is an annotation from `config.annotated_queryset`, which is
    # why it can be sorted on without being a column.
    ordering_fields = ["sort_order", "created_at", "name", "subscriber_count"]
    ordering = ["sort_order", "-created_at"]
    # `sort_order` and `subscriber_count` are mostly ties, and SQL promises nothing
    # about tied rows between two queries — without a unique final key, page 2 can
    # repeat a plan from page 1.
    ordering_tiebreaker = ("-created_at", "id")

    #: Everything that changes the catalogue. Anything not named here is a read.
    WRITE_ACTIONS = frozenset({
        "create", "partial_update", "entitlements", "rates", "activate", "retire",
        "clone",
    })

    @property
    def required_permission(self):
        """What this request needs, resolved from the action.

        **A property, not something a handler sets.** `has_permission` runs during
        `initial()`, before the handler is ever called — assigning it inside the
        handler would check `None` and let any platform staff write, then set the
        attribute far too late to matter.

        `self.action` is populated by DRF's dispatch before permissions are checked,
        so it is available here and is the only thing that can be trusted to say what
        is about to happen.
        """
        return Action.PLAN_CHANGE if self.action in self.WRITE_ACTIONS else None

    def get_queryset(self):
        return config.annotated_queryset()

    def get_plan(self):
        return get_object_or_404(Plan, pk=self.kwargs["pk"], is_deleted=False)

    # -- audit ------------------------------------------------------------- #

    def _record(self, action, reason, plan=None, **kwargs):
        """Every write leaves a row. A catalogue change with no explanation is
        indistinguishable from a bug six weeks later."""
        return operator_audit.record(
            actor=self.request.user,
            action=action,
            reason=reason,
            target=str(plan.pk) if plan else "",
            request=self.request,
            **kwargs,
        )

    def _reason(self):
        """Why this change is being made. Required on every write."""
        reason = (self.request.data or {}).get("reason", "")
        return reason.strip() if isinstance(reason, str) else ""

    def _require_reason(self):
        if not self._reason():
            return self.error(
                message="A reason is required for any change to the catalogue.",
                errors=["REASON_REQUIRED"],
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return None

    # -- read -------------------------------------------------------------- #

    @extend_schema(summary="Every plan, with what is configured on it")
    def list(self, request):
        # This used to serialise `get_queryset()` whole and never call
        # `filter_queryset`, so every filter, `page` and `page_size` were accepted and
        # silently ignored. Filtering stays in `list` only: detail routes resolve
        # through `get_plan()`, so a stray `?status=` cannot 400 a retrieve.
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        # `SetPagination` already emits the project envelope, so its response is
        # returned directly rather than wrapped in a second one.
        return self.get_paginated_response(PlanListSerializer(page, many=True).data)

    @extend_schema(summary="One plan, whole — the editor loads this once")
    def retrieve(self, request, pk=None):
        plan = self.get_plan()
        return self.success(data=self._whole(plan), message="Plan retrieved")

    def _whole(self, plan):
        """All three models plus readiness, in one payload.

        The editor holds the whole plan; three requests to render one screen would
        also mean three chances to render it half-loaded.
        """
        from ops.serializers.plan import (
            CreditRateSerializer,
            PlanEntitlementSerializer,
        )

        return {
            "plan": PlanFieldsSerializer(plan).data,
            "entitlements": PlanEntitlementSerializer(
                config.live_entitlements(plan), many=True
            ).data,
            "rates": CreditRateSerializer(config.live_rates(plan), many=True).data,
            "readiness": readiness.report(plan),
            "subscriber_count": config.subscriber_count(plan),
        }

    @extend_schema(summary="What is missing, and what looks wrong")
    @action(detail=True, methods=["get"])
    def readiness(self, request, pk=None):
        return self.success(
            data=readiness.report(self.get_plan()), message="Readiness computed"
        )

    # -- write ------------------------------------------------------------- #

    @extend_schema(
        summary="Create a plan — all 84 settings, as a draft",
        request=PlanConfigurationSerializer,
    )
    def create(self, request):
        if (refused := self._require_reason()) is not None:
            return refused

        payload = PlanConfigurationSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(
                message="Validation failed",
                errors=payload.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            plan = config.create(
                plan_fields=payload.validated_data["plan"],
                entitlements=payload.validated_data["entitlements"],
                rates=payload.validated_data["rates"],
            )
        except IntegrityError as exc:
            return self._integrity_error(exc, Action.PLAN_CHANGE)

        self._record(
            Action.PLAN_CHANGE, self._reason(), plan=plan,
            payload={"operation": "create", "code": plan.code},
            result={"plan_id": str(plan.pk), "status": plan.status},
        )
        return self.success(
            data=self._whole(plan),
            message=f"{plan.name} created as a draft.",
            status_code=status.HTTP_201_CREATED,
        )

    @extend_schema(summary="Edit the plan's own fields", request=PlanFieldsSerializer)
    def partial_update(self, request, pk=None):
        if (refused := self._require_reason()) is not None:
            return refused

        plan = self.get_plan()
        payload = PlanFieldsSerializer(plan, data=request.data, partial=True)
        if not payload.is_valid():
            return self.error(
                message="Validation failed",
                errors=payload.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            payload.save()
        except IntegrityError as exc:
            return self._integrity_error(exc, Action.PLAN_CHANGE, plan)

        self._record(
            Action.PLAN_CHANGE, self._reason(), plan=plan,
            payload={"operation": "update", "fields": sorted(payload.validated_data)},
        )
        plan.refresh_from_db()
        return self.success(data=self._whole(plan), message="Plan updated")

    @extend_schema(
        summary="Replace the capacity grid — all 7 resources",
        request=PlanEntitlementGridSerializer,
    )
    @action(detail=True, methods=["put"])
    def entitlements(self, request, pk=None):
        if (refused := self._require_reason()) is not None:
            return refused

        plan = self.get_plan()
        payload = PlanEntitlementGridSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(
                message="Validation failed",
                errors=payload.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            config.replace_entitlements(plan, payload.validated_data["entitlements"])
        except IntegrityError as exc:
            return self._integrity_error(exc, Action.PLAN_CHANGE, plan)

        self._record(
            Action.PLAN_CHANGE, self._reason(), plan=plan,
            payload={"operation": "entitlements"},
        )
        return self.success(data=self._whole(plan), message="Capacity updated")

    @extend_schema(
        summary="Replace the rate card — all 3 tiers",
        request=CreditRateCardSerializer,
    )
    @action(detail=True, methods=["put"])
    def rates(self, request, pk=None):
        if (refused := self._require_reason()) is not None:
            return refused

        plan = self.get_plan()
        payload = CreditRateCardSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(
                message="Validation failed",
                errors=payload.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # A draft has no subscriber whose wallet carries a stamp of these rates, so
        # there is no history to preserve and a version boundary would mean nothing.
        # A live plan supersedes, which is what keeps historical charges reproducible.
        supersede = plan.status != Plan.Status.DRAFT

        try:
            config.replace_rates(
                plan, payload.validated_data["rates"], supersede=supersede
            )
        except config.RateWouldStrandBasis as exc:
            # A live plan sold by the minute reads its price and its grant from one
            # rate row. Blanking that row here would break a plan already on sale,
            # which is the door activation cannot watch.
            return self.error(
                message=str(exc),
                errors=["RATE_WOULD_STRAND_BASIS"],
                status_code=status.HTTP_409_CONFLICT,
            )
        except IntegrityError as exc:
            return self._integrity_error(exc, Action.PLAN_CHANGE, plan)

        self._record(
            Action.PLAN_CHANGE, self._reason(), plan=plan,
            payload={"operation": "rates", "superseded": supersede},
        )
        return self.success(
            data=self._whole(plan),
            message="Rate card superseded" if supersede else "Rate card updated",
        )

    # -- lifecycle --------------------------------------------------------- #

    @extend_schema(summary="Make a plan sellable. Refused unless complete", request=None)
    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        if (refused := self._require_reason()) is not None:
            return refused

        plan = self.get_plan()
        try:
            lifecycle.activate(plan)
        except lifecycle.PlanNotReady as exc:
            self._record(
                Action.PLAN_CHANGE, self._reason(), plan=plan,
                payload={"operation": "activate"},
                outcome=OperatorAuditLog.Outcome.FAILED,
                result={"blocking": exc.problems},
            )
            return self.error(
                message=str(exc),
                errors=["PLAN_NOT_READY"],
                data={"blocking": exc.problems},
                status_code=status.HTTP_409_CONFLICT,
            )

        self._record(
            Action.PLAN_CHANGE, self._reason(), plan=plan,
            payload={"operation": "activate"},
            result={"status": plan.status},
        )
        return self.success(
            data=self._whole(plan), message=f"{plan.name} is now live."
        )

    @extend_schema(
        summary="Withdraw a plan. Stops new subscriptions only", request=None
    )
    @action(detail=True, methods=["post"])
    def retire(self, request, pk=None):
        if (refused := self._require_reason()) is not None:
            return refused

        plan = self.get_plan()
        result = lifecycle.retire(plan)
        remaining = result["subscribers_remaining"]

        self._record(
            Action.PLAN_CHANGE, self._reason(), plan=plan,
            payload={"operation": "retire"},
            result={"status": plan.status, "subscribers_remaining": remaining},
        )
        return self.success(
            data=self._whole(plan),
            message=(
                f"{plan.name} withdrawn. {remaining} tenant(s) stay on it and keep "
                "their current terms."
            ),
        )

    @extend_schema(
        summary="Duplicate a plan as a new draft",
        request=PlanCloneSerializer,
    )
    @action(detail=True, methods=["post"])
    def clone(self, request, pk=None):
        """Copy a plan, all three models, as a draft.

        The reason this exists rather than "copy the fields by hand": a yearly plan is
        a separate plan with 31 mostly-identical fields, and retyping them is how one
        ends up carrying a monthly plan's add-on prices — the same capacity for a
        twelfth of the money.
        """
        if (refused := self._require_reason()) is not None:
            return refused

        source = self.get_plan()
        payload = PlanCloneSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(
                message="Validation failed",
                errors=payload.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            plan, review = clone_service.clone(source, **payload.validated_data)
        except IntegrityError as exc:
            return self._integrity_error(exc, Action.PLAN_CHANGE, source)

        self._record(
            Action.PLAN_CHANGE, self._reason(), plan=plan,
            payload={"operation": "clone", "source": str(source.pk)},
            result={"plan_id": str(plan.pk), "review": review},
        )
        body = self._whole(plan)
        body["cloned_from"] = {"id": str(source.pk), "name": source.name}
        body["review"] = review
        return self.success(
            data=body,
            message=f"{plan.name} created as a draft, copied from {source.name}.",
            status_code=status.HTTP_201_CREATED,
        )

    # -- shared error shaping ----------------------------------------------- #

    def _integrity_error(self, exc, action, plan=None):
        """Turn a database constraint into a sentence.

        The serializers catch these first, so reaching here means a combination the
        validation does not know about — worth logging rather than reporting as a
        validation failure that names no field.
        """
        logger.exception("plan write refused by the database")
        self._record(
            action, self._reason() or "(none given)", plan=plan,
            outcome=OperatorAuditLog.Outcome.FAILED,
            result={"error": str(exc)},
        )
        return self.error(
            message=(
                "The database refused this combination of settings. It is a valid "
                "shape but not a valid plan; check the constraint named below."
            ),
            errors=["CONSTRAINT_VIOLATION"],
            data={"detail": str(exc)},
            status_code=status.HTTP_409_CONFLICT,
        )
