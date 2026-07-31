"""
Tenant scoping for VB_AGENT (machine) callers.

VB_AGENT users have no `TenantUserMapping` — the role is global by design — so
`TenantBaseViewSet` cannot resolve a tenant for them and every VB_AGENT path has
had to opt out of tenant scoping entirely.

This module narrows that. A machine caller may name the tenant it is working on
with the `X-Tenant-ID` header, and the VB_AGENT branches scope their querysets to
it.

The header is MANDATORY on every endpoint a VB_AGENT token can reach. Omitting it is a
400, never a global read — an unscoped machine call is precisely the cross-tenant leak
this exists to prevent.

It is also the sole source of `tenant` on anything a machine caller creates. That is not
just a scoping detail: a record written without the right tenant is invisible to the
humans who own it, so a booking the voicebot made would simply never appear in their
list.

WHAT THIS IS, AND WHAT IT IS NOT — read before relying on it.
`AgentToken` carries no tenant, so this header is a FILTER, not an authorization
check: nothing prevents a token holder from naming any tenant it likes. It stops a
misconfigured or buggy runner from reading and writing the wrong tenant, and it
makes the intended scope explicit and auditable in every request. It does NOT contain a
leaked token — that requires the allowed tenants to live on `AgentToken`, at which point
the header becomes a selector among them and `resolve_agent_tenant` below is the
single function that changes.
"""

import uuid

from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import OpenApiParameter
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import BasePermission

from tenant.models.Tenant import Tenant
from user.models.CustomUser import CustomUser

# The header clients send. `X-Tenant-ID` rather than a query parameter: several of
# these viewsets declare `filterset_fields`, where a `?tenant=` would collide with
# django-filter, and a header applies uniformly to GET and POST without needing to
# be threaded through pagination links.
AGENT_TENANT_HEADER = "X-Tenant-ID"
_AGENT_TENANT_META_KEY = "HTTP_X_TENANT_ID"

# Resolution is cached per request: DRF calls get_queryset more than once per
# dispatch (list, pagination, get_object), and this would otherwise be a database
# query each time.
_RESOLVED_ATTR = "_vb_agent_tenant"


def is_vb_agent(user):
    """True when the caller authenticated as a machine (voicebot) user."""
    return getattr(user, "user_type", None) == CustomUser.UserType.VB_AGENT


def resolve_agent_tenant(request):
    """
    The tenant a VB_AGENT caller named via `X-Tenant-ID`, or None.

    None means "do not narrow" — either the caller is not a VB_AGENT (ordinary
    users are scoped by their tenant mapping, never by a header they control) or
    no header was sent.

    Raises ValidationError for a malformed id and ValidationError for one that
    does not resolve to an active tenant, so a typo surfaces as a clear 400 rather
    than as silently empty results.
    """
    if hasattr(request, _RESOLVED_ATTR):
        return getattr(request, _RESOLVED_ATTR)

    tenant = _resolve_uncached(request)
    setattr(request, _RESOLVED_ATTR, tenant)
    return tenant


def _resolve_uncached(request):
    if not is_vb_agent(getattr(request, "user", None)):
        return None

    raw = (request.META.get(_AGENT_TENANT_META_KEY) or "").strip()
    if not raw:
        return None

    try:
        tenant_id = uuid.UUID(raw)
    except (AttributeError, TypeError, ValueError):
        raise ValidationError(
            {AGENT_TENANT_HEADER: "Must be a valid tenant UUID."}
        )

    tenant = Tenant.objects.filter(id=tenant_id, is_deleted=False).first()
    if tenant is None:
        raise ValidationError(
            {AGENT_TENANT_HEADER: "No active tenant matches this id."}
        )

    return tenant


def require_agent_tenant(request):
    """
    The tenant a VB_AGENT caller must name, or None for ordinary users.

    Unlike `resolve_agent_tenant`, an absent header is an error here. Use this on
    endpoints a machine caller reaches *only* through the header — booking writes have
    to land in some tenant, and a machine caller has no mapping to fall back on, so
    "unscoped" is not a coherent option the way it is for a global read.

    Ordinary users still get None: their scope comes from their tenant mapping, and a
    header they control must never participate in it.
    """
    if not is_vb_agent(getattr(request, "user", None)):
        return None

    tenant = resolve_agent_tenant(request)
    if tenant is None:
        raise ValidationError(
            {AGENT_TENANT_HEADER: "This header is required for agent tokens."}
        )
    return tenant


AGENT_TENANT_HEADER_PARAMETER = OpenApiParameter(
    name=AGENT_TENANT_HEADER,
    type=str,
    location=OpenApiParameter.HEADER,
    required=False,  # Only agent tokens must send it; a human JWT never does.
    description=(
        "**Required for `VB_AGENT` (agent token) callers, ignored for JWT users.** "
        "The tenant this request acts on. Agent tokens have no tenant mapping, so this "
        "header is what scopes every read and what is written to `tenant` on anything "
        "created. Omitting it returns 400."
    ),
)


class VBAgentAwareAutoSchema(AutoSchema):
    """
    Documents `X-Tenant-ID` on every operation an agent token can call.

    Driven off `vb_agent_allowed_actions` — the same attribute the permission enforces —
    rather than a hand-maintained list, so the published schema cannot drift from what
    the API actually accepts. Adding an endpoint to the surface documents it for free.
    """

    def _get_parameters(self):
        parameters = super()._get_parameters()

        allowed = getattr(self.view, "vb_agent_allowed_actions", None)
        if allowed and getattr(self.view, "action", None) in allowed:
            # Manually build the resolved OpenAPI parameter dictionary
            resolved_param = {
                "name": AGENT_TENANT_HEADER_PARAMETER.name,
                "in": "header",
                "required": AGENT_TENANT_HEADER_PARAMETER.required,
                "description": AGENT_TENANT_HEADER_PARAMETER.description,
                "schema": {"type": "string"},
            }
            if not any(p.get("name") == resolved_param["name"] and p.get("in") == resolved_param["in"] for p in parameters):
                parameters.append(resolved_param)

        return parameters


class VBAgentActionPermission(BasePermission):
    """
    Restricts machine callers to an explicit per-viewset action allowlist.

    Ordinary users pass straight through — this class only ever narrows VB_AGENT.

    DENIES BY DEFAULT. A view that declares no `vb_agent_allowed_actions` refuses
    machine callers outright, so a new endpoint is closed to tokens until someone opts
    it in, rather than being open by whatever its tenant scoping happens to permit. That
    default is the point: the previous surface grew silently because
    `AgentTokenAuthentication` is a global auth class, so every new viewset admitted
    tokens the moment it was written.

    Declare the allowlist with DRF action names — the method name for @action routes
    (`no_show`, not `no-show`).
    """

    message = "This endpoint is not available to agent tokens."

    def has_permission(self, request, view):
        if not is_vb_agent(getattr(request, "user", None)):
            return True

        allowed = getattr(view, "vb_agent_allowed_actions", None)
        if not allowed:
            return False

        return getattr(view, "action", None) in allowed


def assert_agent_in_tenant(agent, tenant):
    """
    A named `agent` must belong to the tenant the caller named.

    Without this the `agent` field alone decides the tenant of a write, which lets
    a caller land call logs and recordings in any tenant by naming that tenant's
    agent id. When a header is present the two must agree.

    Reported as 403 rather than 404. The file's object-key checks use 404 to avoid
    confirming what exists in another tenant, but that reasoning does not carry
    here: the header grants nothing and the caller can simply omit it, so hiding
    the mismatch buys no secrecy and costs the client an actionable error.

    No-ops when `tenant` is None, i.e. when the caller sent no header.
    """
    if tenant is None:
        return
    if agent.tenant_id != tenant.id:
        raise PermissionDenied(
            "The specified agent does not belong to the requested tenant."
        )


class VBAgentTenantScopedMixin:
    """
    Applies the mandatory `X-Tenant-ID` scope to a viewset's VB_AGENT branch.

    There is deliberately no "optional header" mode. An unscoped machine call is exactly
    the cross-tenant leak this exists to prevent, and a mode flag would be one attribute
    away from reintroducing it. Every VB_AGENT request names its tenant or gets a 400.
    """

    def get_agent_tenant(self):
        return require_agent_tenant(self.request)

    def scope_to_agent_tenant(self, queryset):
        """Narrow an already-built queryset. A no-op without a resolved tenant."""
        tenant = self.get_agent_tenant()
        if tenant is None:
            return queryset
        return queryset.filter(tenant=tenant)

    def agent_scoped_queryset(self):
        """
        The tenant-scoped queryset for a machine caller, or None if this caller is not
        one (in which case the viewset should fall through to its normal scoping).

        Deliberately routed around `TenantBaseViewSet.get_queryset`, which raises for
        any caller without a tenant mapping — the whole reason machine callers could not
        reach these endpoints before. `BaseViewSet.get_queryset` still supplies the
        `is_deleted=False` filter and the select/prefetch configuration.
        """
        # Inline by necessity, not oversight (AGENTS.md §8 allows the verified-cycle
        # case): base_model_viewset imports VBAgentActionPermission from this module for
        # BaseViewSet.permission_classes, so a top-level import here is a real cycle.
        from utils.views.base_model_viewset import BaseViewSet

        if not is_vb_agent(getattr(self.request, "user", None)):
            return None

        tenant = self.get_agent_tenant()
        if tenant is None:
            return None

        return BaseViewSet.get_queryset(self).filter(tenant=tenant)
