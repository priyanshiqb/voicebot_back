"""Tenant list and the 360 — the screen support opens when a customer emails.

A thin layer, like the plan viewset: everything with a rule in it lives in
``ops.services.tenant``, and these methods validate, call a service, write an audit
row and shape a response.

**Not built on the tenant-scoped viewsets.** ``TenantBaseViewSet.get_queryset()``
resolves the tenant from ``request.user.tenant_mapping`` and a platform operator has
none — the ``ck_platform_role_requires_admin_user_type`` constraint and the 1:1
mapping guarantee it. The failure mode is not an error: that viewset would be doing
exactly what it was built to do, having been asked the wrong question. Here the tenant
is a **path parameter**, every time.
"""

import logging

from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.viewsets import GenericViewSet

from ops.permissions import HasConsolePermission
from ops.serializers.tenant import (
    EnforcementSerializer,
    TenantListSerializer,
    TenantProfileWriteSerializer,
    render_detail,
)
from ops.services.tenant import detail as detail_service
from ops.services.tenant import health as health_service
from ops.services.tenant import listing as listing_service
from tenant.models import Tenant, TenantSettings
from user.models import OperatorAuditLog
from user.services import operator_audit
from utils.views.base_model_viewset import ResponseMixin, SetPagination

logger = logging.getLogger(__name__)

Action = OperatorAuditLog.Action


@extend_schema(tags=["Ops - Tenants"])
class TenantConsoleViewSet(ResponseMixin, GenericViewSet):
    """Every tenant, and everything about one of them.

    Reads require only platform staff — a console where support cannot look at a
    tenant is not a console. Writes name their permission and leave an audit row.
    """

    permission_classes = (HasConsolePermission,)
    serializer_class = TenantListSerializer
    pagination_class = SetPagination

    #: What each write needs. Anything not named here is a read, which the namespace
    #: gate has already covered — so a forgotten entry is "readable by staff", never
    #: "open to the world".
    WRITE_PERMISSIONS = {
        "partial_update": Action.TENANT_PROFILE,
        "enforcement": Action.ENFORCEMENT_CHANGE,
    }

    @property
    def required_permission(self):
        """What this request needs, resolved from the action.

        **A property, not something a handler sets.** ``has_permission`` runs during
        ``initial()``, before the handler is ever called — assigning it inside the
        handler would check ``None``, let any platform staff write, and set the
        attribute far too late to matter.
        """
        return self.WRITE_PERMISSIONS.get(self.action)

    def get_queryset(self):
        return listing_service.build(self.request.query_params)

    def get_tenant(self):
        """One tenant, by path parameter. Soft-deleted rows included.

        "This tenant says their account vanished" is a question the console exists to
        answer, so a deleted tenant is readable. Writes then refuse it explicitly with
        ``TENANT_DELETED`` rather than 404ing: the console has just rendered this
        tenant on a read, and telling an operator it does not exist when they try to
        change it is the confusing half of both answers.
        """
        return get_object_or_404(
            Tenant.all_objects.select_related("timezone", "language"),
            pk=self.kwargs["pk"],
        )

    # -- audit -------------------------------------------------------------- #

    def _record(self, action, reason, tenant=None, **kwargs):
        return operator_audit.record(
            actor=self.request.user,
            action=action,
            reason=reason,
            tenant=tenant,
            target=str(tenant.pk) if tenant else "",
            request=self.request,
            **kwargs,
        )

    def _reason(self):
        reason = (self.request.data or {}).get("reason", "")
        return reason.strip() if isinstance(reason, str) else ""

    def _require_reason(self):
        if not self._reason():
            return self.error(
                message="A reason is required for any change to a tenant.",
                errors=["REASON_REQUIRED"],
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return None

    def _refuse_deleted(self, tenant):
        """No write touches a deleted tenant.

        Restoring one is a lifecycle action with its own decision behind it, not
        something a name change should do as a side effect.
        """
        if tenant.is_deleted:
            return self.error(
                message="This tenant is deleted. Restore it before changing it.",
                errors=["TENANT_DELETED"],
                status_code=status.HTTP_409_CONFLICT,
            )
        return None

    # -- read --------------------------------------------------------------- #

    @extend_schema(summary="Every tenant, with the columns the list screen needs")
    def list(self, request):
        page = self.paginate_queryset(self.get_queryset())
        serializer = TenantListSerializer(page, many=True)
        # `SetPagination` already emits the project envelope, so this returns its
        # response directly rather than wrapping an envelope in an envelope.
        return self.get_paginated_response(serializer.data)

    @extend_schema(summary="One tenant, whole — the 360")
    def retrieve(self, request, pk=None):
        tenant = self.get_tenant()
        return self.success(
            data=render_detail(detail_service.assemble(tenant)),
            message="Tenant retrieved",
        )

    @extend_schema(summary="Is anything wrong with this tenant?")
    @action(detail=True, methods=["get"])
    def health(self, request, pk=None):
        tenant = self.get_tenant()
        return self.success(
            data=health_service.report(tenant.id), message="Health computed"
        )

    # -- write -------------------------------------------------------------- #

    @extend_schema(
        summary="Correct a tenant's name, timezone or language",
        request=TenantProfileWriteSerializer,
    )
    def partial_update(self, request, pk=None):
        if (refused := self._require_reason()) is not None:
            return refused

        tenant = self.get_tenant()
        if (refused := self._refuse_deleted(tenant)) is not None:
            return refused

        payload = TenantProfileWriteSerializer(tenant, data=request.data, partial=True)
        if not payload.is_valid():
            return self.error(
                message="Validation failed",
                errors=payload.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        before = {
            "name": tenant.name,
            "timezone": str(tenant.timezone_id) if tenant.timezone_id else None,
            "language": str(tenant.language_id) if tenant.language_id else None,
        }
        try:
            payload.save()
        except IntegrityError as exc:
            return self._integrity_error(exc, Action.TENANT_PROFILE, tenant)

        self._record(
            Action.TENANT_PROFILE, self._reason(), tenant=tenant,
            payload={"fields": sorted(payload.validated_data), "before": before},
        )
        tenant.refresh_from_db()
        return self.success(
            data=render_detail(detail_service.assemble(tenant)),
            message="Tenant updated",
        )

    @extend_schema(
        summary="Turn credit and entitlement enforcement on or off",
        request=EnforcementSerializer,
    )
    @action(detail=True, methods=["post"])
    def enforcement(self, request, pk=None):
        """Both switches default to off platform-wide, deliberately.

        Metering runs regardless — every call is rated and written to the ledger from
        day one — and enforcement is what turns a shortfall into a refusal. Switching
        it on for a tenant already over their limits breaks a working account with no
        warning, so it is a per-tenant decision somebody makes after looking at the
        usage on this very screen.
        """
        if (refused := self._require_reason()) is not None:
            return refused

        tenant = self.get_tenant()
        if (refused := self._refuse_deleted(tenant)) is not None:
            return refused

        payload = EnforcementSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(
                message="Validation failed",
                errors=payload.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # The one place a write may create the settings row: `for_tenant` is banned on
        # read paths because it is a `get_or_create`, but a tenant being given an
        # enforcement setting is exactly when the row should come into existence.
        settings_row = TenantSettings.for_tenant(tenant.id)
        before = {
            "credits": settings_row.credits_enforced,
            "entitlements": settings_row.entitlements_enforced,
        }

        changed = []
        if "credits" in payload.validated_data:
            settings_row.credits_enforced = payload.validated_data["credits"]
            changed.append("credits_enforced")
        if "entitlements" in payload.validated_data:
            settings_row.entitlements_enforced = payload.validated_data["entitlements"]
            changed.append("entitlements_enforced")
        settings_row.save(update_fields=changed + ["updated_at"])

        self._record(
            Action.ENFORCEMENT_CHANGE, self._reason(), tenant=tenant,
            payload={"before": before, "requested": payload.validated_data},
            result={
                "credits_enforced": settings_row.credits_enforced,
                "entitlements_enforced": settings_row.entitlements_enforced,
            },
        )
        return self.success(
            data=render_detail(detail_service.assemble(tenant)),
            message="Enforcement updated",
        )

    # -- shared error shaping ------------------------------------------------ #

    def _integrity_error(self, exc, action, tenant=None):
        """Turn a database constraint into a sentence.

        The serializers catch these first, so reaching here means a combination the
        validation does not know about — worth logging rather than reporting as a
        validation failure that names no field.
        """
        logger.exception("tenant write refused by the database")
        self._record(
            action, self._reason() or "(none given)", tenant=tenant,
            outcome=OperatorAuditLog.Outcome.FAILED,
            result={"error": str(exc)},
        )
        return self.error(
            message=(
                "The database refused this change. It is a valid shape but not a "
                "valid tenant; check the constraint named below."
            ),
            errors=["CONSTRAINT_VIOLATION"],
            data={"detail": str(exc)},
            status_code=status.HTTP_409_CONFLICT,
        )
