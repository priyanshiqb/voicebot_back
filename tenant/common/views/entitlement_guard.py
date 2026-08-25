"""Enforcing plan limits at the point of creation."""

import logging

from rest_framework.exceptions import APIException
from rest_framework import status

logger = logging.getLogger(__name__)


class QuotaExceededError(APIException):
    """402, not 403.

    403 means "you may not do this"; this is "you may, but you have used up what you
    bought" — a billing state the tenant can resolve themselves by upgrading or buying
    capacity. Same status the wallet uses when credits run out, so the frontend has
    one code to handle for "you need to pay for more".
    """

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_code = "QUOTA_EXCEEDED"


class EntitlementGuardMixin:
    """Refuse creates that would take a tenant past their plan limit.

    Opt in by setting ``entitlement_resource`` on the viewset::

        class AgentViewSet(EntitlementGuardMixin, TenantBaseViewSet):
            entitlement_resource = EntitlementResource.AGENT

    Checked in ``perform_create`` rather than in a serializer: a serializer can be
    reused by an importer or a management command where a plan limit is not the right
    gate, whereas this is specifically about a tenant creating something through the
    API.

    **A viewset that overrides ``perform_create`` without calling ``super()`` silently
    loses the guard.** That is ordinary Python, but it is invisible — the endpoint
    keeps working and simply stops enforcing. ``CustomUserViewSet`` is one such case
    and calls :meth:`check_entitlement` directly. If you add a guard to a viewset,
    check whether it defines its own ``perform_create`` first.
    """

    #: The EntitlementResource this viewset consumes. None disables the guard.
    entitlement_resource = None

    def entitlement_requests(self, serializer):
        """What this create consumes, as ``{resource: count}``.

        Override where one request consumes more than one thing — creating a knowledge
        base with fifty websites uses one knowledge base *and* fifty crawl pages, and
        checking only the container would let a tenant walk past their page limit by
        putting everything in one KB.
        """
        if self.entitlement_resource:
            return {self.entitlement_resource: 1}
        return {}

    def check_entitlement(self, resource, requested=1):
        from onboard.services.entitlements import QuotaExceeded, check

        try:
            tenant = self.request.user.tenant_mapping.tenant
        except Exception:
            # No tenant mapping means tenant scoping has already refused this request,
            # or the caller is a superuser. Either way there is no plan to check.
            return

        try:
            check(tenant, resource, requested=requested)
        except QuotaExceeded as exc:
            noun = exc.resource.replace("_", " ")
            raise QuotaExceededError({
                "message": (
                    f"You are using {exc.used} of {exc.limit} available {noun}"
                    + (f", and this would add {exc.requested} more" if exc.requested > 1 else "")
                    + ". Upgrade your plan or buy more capacity to continue."
                ),
                "errors": ["QUOTA_EXCEEDED"],
                "data": {
                    "resource": exc.resource,
                    "used": exc.used,
                    "limit": exc.limit,
                    "requested": exc.requested,
                },
            })

    def perform_create(self, serializer, **kwargs):
        for resource, count in self.entitlement_requests(serializer).items():
            if count > 0:
                self.check_entitlement(resource, requested=count)
        return super().perform_create(serializer, **kwargs)
