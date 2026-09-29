"""Manual onboarding: the ``onboard_custom_deal`` command as a Django admin screen.

The list shows every tenant onboarded by hand, and who did it. **Add** walks the same
steps the command prompts for -- plan, business, payment, terms, allowances -- then a
review, and creates only after the operator enters a fresh code from their
authenticator app.

Who may use it: superusers, and staff holding "Can add manual onboarding" (grant it
under Users or Groups). Seeing the list needs "Can view manual onboarding".

Every attempt is written to the operator audit log as ``tenant.onboard``, with the
operator, the reason they gave, what was requested (never the password), the client
IP and user agent: successes, failed authenticator codes, and provisioning errors
alike. Nothing here can be edited or deleted -- the subscription is changed through its
own admin, and the audit log is append-only.
"""

import logging

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db.models import OuterRef, Subquery
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html

from onboard.forms.manual_onboarding import ManualOnboardingForm, PlanStepForm
from onboard.models import ManualOnboarding
from onboard.services.manual_onboarding import provision
from user.models import OperatorAuditLog
from user.services import operator_audit
from user.services.step_up import StepUpError, verify_totp
from utils.admin.base_admin_core import BaseAdminCore

logger = logging.getLogger(__name__)

AUDIT_ACTION = OperatorAuditLog.Action.TENANT_ONBOARD
TEMPLATES = "admin/onboard/manualonboarding/"


@admin.register(ManualOnboarding)
class ManualOnboardingAdmin(BaseAdminCore):
    model = ManualOnboarding
    actions = None

    list_display = (
        "tenant",
        "plan",
        "amount_paid",
        "payment_reference",
        "onboarded_by",
        "status",
        "start_date",
        "end_date",
    )
    list_filter = ("status", "plan", "start_date")
    search_fields = (
        "tenant__name",
        "tenant__subdomain",
        "plan__name",
        "plan__code",
        "plan_snapshot__manual_payment_reference",
    )
    list_select_related = ("tenant", "plan")
    ordering = ("-created_at",)

    fieldsets = (
        ("Manual onboarding", {
            "fields": (
                "tenant", "plan", "status", "start_date", "end_date",
                "amount_paid", "payment_reference", "onboarded_by", "related_links",
            ),
        }),
        ("Frozen contract", {
            "fields": ("plan_snapshot",),
            "classes": ("collapse",),
            "description": (
                "The negotiated terms as written to the subscription. Allowances and "
                "credits are read from here, never from the plan."
            ),
        }),
    )

    def get_queryset(self, request):
        # The operator comes from the audit log rather than a column on the
        # subscription, so there is exactly one record of who did it. One subquery,
        # not a lookup per row.
        onboarded_by = (
            OperatorAuditLog.objects.filter(
                action=AUDIT_ACTION,
                outcome=OperatorAuditLog.Outcome.SUCCESS,
                tenant=OuterRef("tenant"),
            )
            .order_by("created_at")
            .values("actor__email")[:1]
        )
        return (
            super()
            .get_queryset(request)
            .filter(plan_snapshot__has_key="manual_payment_reference")
            .annotate(onboarded_by_email=Subquery(onboarded_by))
        )

    # --- Columns -------------------------------------------------------------------

    @admin.display(description="Amount paid")
    def amount_paid(self, obj):
        amount = (obj.plan_snapshot or {}).get("total_price")
        return f"{obj.plan.currency} {amount}" if amount is not None else "—"

    @admin.display(description="Payment reference")
    def payment_reference(self, obj):
        return (obj.plan_snapshot or {}).get("manual_payment_reference") or "—"

    @admin.display(description="Onboarded by", ordering="onboarded_by_email")
    def onboarded_by(self, obj):
        # Command-line runs are not attributed to anyone: there is no signed-in user.
        return getattr(obj, "onboarded_by_email", None) or "— (command line)"

    @admin.display(description="Open")
    def related_links(self, obj):
        audit = (
            reverse("admin:user_operatorauditlog_changelist")
            + f"?action__exact={AUDIT_ACTION}&q={obj.tenant.subdomain}"
        )
        return format_html(
            '<a href="{}">Subscription</a> · <a href="{}">Tenant</a> · '
            '<a href="{}">Audit log</a>',
            reverse("admin:onboard_subscription_change", args=[obj.pk]),
            reverse("admin:tenant_tenant_change", args=[obj.tenant_id]),
            audit,
        )

    # --- Read-only everywhere except Add ------------------------------------------

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields] + [
            "amount_paid", "payment_reference", "onboarded_by", "related_links",
        ]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    # --- Add: the onboarding flow --------------------------------------------------

    def add_view(self, request, form_url="", extra_context=None):
        if not self.has_add_permission(request):
            raise PermissionDenied

        if request.method == "POST":
            return self._details_step(request, PlanStepForm(request.POST))
        if "plan" in request.GET:
            return self._details_step(request, PlanStepForm(request.GET))
        return self._render(
            request, "select_plan.html",
            title="Manual onboarding: choose a plan", plan_form=PlanStepForm(),
        )

    def _details_step(self, request, plan_form):
        if not plan_form.is_valid():
            return self._render(
                request, "select_plan.html",
                title="Manual onboarding: choose a plan", plan_form=plan_form,
            )
        plan = plan_form.cleaned_data["plan"]
        minutes = plan_form.cleaned_data["minutes"]

        if request.method != "POST":
            form = ManualOnboardingForm(plan=plan, minutes=minutes)
            return self._render_form(request, plan_form, form)

        form = ManualOnboardingForm(request.POST, plan=plan, minutes=minutes)
        if not form.is_valid():
            return self._render_form(request, plan_form, form)

        # Review, and anything edited since the review, goes back to the review. Only
        # a Create whose values match what was reviewed goes on to the code.
        if request.POST.get("_action") != "create":
            return self._render_form(request, plan_form, form, review=True)
        if request.POST.get("review_digest") != form.review_digest():
            return self._render_form(
                request, plan_form, form, review=True,
                notice="Something changed since the review. Check it and confirm again.",
            )

        reason = form.cleaned_data["reason"]
        token = form.cleaned_data["otp_token"]
        try:
            device = verify_totp(request.user, token)
        except StepUpError as exc:
            form.add_error("otp_token", str(exc))
            # An empty box is a slip, not an event. A wrong code is recorded: repeated
            # failures on a customer-creating action are exactly the signal to keep.
            if token:
                operator_audit.failed(
                    actor=request.user, action=AUDIT_ACTION, reason=reason,
                    error=f"Authenticator code rejected: {exc}",
                    payload=form.audit_payload(), request=request,
                )
            return self._render_form(request, plan_form, form, review=True)

        try:
            subscription = provision(
                registrant=form.registrant(),
                address=form.address(),
                plan=plan,
                payment=form.payment(),
                terms=form.terms(),
                entitlements=form.entitlements(),
                minutes=minutes,
            )
        except Exception as exc:
            # provision() is atomic, so nothing was written. Kept broad on purpose: the
            # operator needs the reason on screen, and the audit log needs the attempt.
            logger.exception("manual onboarding failed for %s", form.cleaned_data["email"])
            operator_audit.failed(
                actor=request.user, action=AUDIT_ACTION, reason=reason, error=exc,
                payload=form.audit_payload(), request=request,
            )
            form.add_error(None, f"Onboarding failed and nothing was written: {exc}")
            return self._render_form(request, plan_form, form, review=True)

        tenant = subscription.tenant
        operator_audit.record(
            actor=request.user,
            action=AUDIT_ACTION,
            reason=reason,
            tenant=tenant,
            target=subscription.pk,
            payload={**form.audit_payload(), "verified_with": device.name},
            result={
                "tenant_id": str(tenant.pk),
                "subdomain": tenant.subdomain,
                "subscription_id": str(subscription.pk),
                "end_date": subscription.end_date.isoformat(),
            },
            request=request,
        )
        logger.info(
            "manual onboarding: %s onboarded tenant %s (%s)",
            request.user.email, tenant.pk, tenant.subdomain,
        )
        return self._render(
            request, "done.html",
            title="Manual onboarding: done",
            subscription=subscription,
            tenant=tenant,
            sign_in_email=form.cleaned_data["email"],
            # Shown once, on this page only. It is never stored in clear or logged.
            generated_password=(
                form.cleaned_data["password"] if form.generated_password else None
            ),
        )

    # --- Rendering -----------------------------------------------------------------

    def _render_form(self, request, plan_form, form, review=False, notice=None):
        context = {
            "title": "Manual onboarding: details",
            "plan_form": plan_form,
            "form": form,
            "plan": form.plan,
            "minutes": form.minutes,
            "notice": notice,
            "review": form.review_sections() if review else None,
            "review_digest": form.review_digest() if review else "",
        }
        return self._render(request, "details.html", **context)

    def _render(self, request, template, **context):
        opts = self.model._meta
        request.current_app = self.admin_site.name
        return TemplateResponse(request, TEMPLATES + template, {
            **self.admin_site.each_context(request),
            "opts": opts,
            "app_label": opts.app_label,
            "has_view_permission": self.has_view_permission(request),
            **context,
        })
