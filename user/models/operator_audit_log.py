"""A record of what platform staff did, and why.

**Built before the actions it records, deliberately.** Retrofitting who-did-what is
impossible: the moment an operator grants credits without a row here, that grant is
indistinguishable from a bug, and no later migration can reconstruct the intent.

Append-only, on the same reasoning as ``CreditLedger`` and by the same mechanism. An
audit trail that can be edited is not one — and the failure is silent, because a
tampered row looks exactly like an honest one.
"""

from django.conf import settings
from django.db import models

from utils.models.base_model_core import BaseModelCore

_IMMUTABLE = (
    "OperatorAuditLog is append-only. Correct a mistaken action by taking a "
    "compensating one, which leaves its own row."
)


class OperatorAuditLogQuerySet(models.QuerySet):
    """Blocks the three routes that would rewrite history.

    ``soft_delete()`` matters as much as ``delete()``: it issues a plain
    ``UPDATE is_deleted = true``, and because the default manager filters those out,
    a soft-deleted row would vanish from every audit view while still sitting in the
    table. That is worse than a hard delete, which at least leaves a gap in the ids.
    """

    def delete(self):
        raise NotImplementedError(_IMMUTABLE)

    def soft_delete(self):
        raise NotImplementedError(_IMMUTABLE)

    def update(self, **kwargs):
        raise NotImplementedError(_IMMUTABLE)


class OperatorAuditLogManager(models.Manager):
    def get_queryset(self):
        return OperatorAuditLogQuerySet(self.model, using=self._db).filter(
            is_deleted=False
        )


class OperatorAuditLog(BaseModelCore):
    """One thing a platform operator did to one tenant."""

    class Action(models.TextChoices):
        # Support
        CREDITS_GRANT = "credits.grant", "Granted credits"
        CREDITS_DEDUCT = "credits.deduct", "Deducted credits"
        WALLET_FREEZE = "wallet.freeze", "Froze wallet"
        WALLET_UNFREEZE = "wallet.unfreeze", "Unfroze wallet"
        SUBSCRIPTION_EXTEND = "subscription.extend", "Extended cycle"
        PAYMENT_REPLAY = "payment.replay", "Replayed a payment"
        PAYMENT_CANCEL = "payment.cancel", "Cancelled an order"
        WEBHOOK_REPROCESS = "webhook.reprocess", "Reprocessed a webhook event"
        # Platform
        ENFORCEMENT_CHANGE = "enforcement.change", "Changed enforcement"
        TENANT_ONBOARD = "tenant.onboard", "Onboarded a tenant manually"
        TENANT_SUSPEND = "tenant.suspend", "Suspended a tenant"
        # Commercial
        PLAN_CHANGE = "plan.change", "Edited the catalogue"

    class Outcome(models.TextChoices):
        SUCCESS = "success", "Succeeded"
        FAILED = "failed", "Failed"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="operator_actions",
        help_text=(
            "The platform user who did this. PROTECT, not CASCADE: deleting a "
            "staff account must never erase what they did."
        ),
    )
    action = models.CharField(
        max_length=32,
        choices=Action.choices,
        db_index=True,
        help_text="What was done.",
    )
    tenant = models.ForeignKey(
        "tenant.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="operator_actions",
        help_text=(
            "Who it was done to. Null for actions with no single tenant, such as "
            "editing the plan catalogue."
        ),
    )
    target = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        help_text=(
            "The specific thing acted on, where one exists — an order id, a "
            "subscription id. Free text rather than a generic relation: the point is "
            "to be readable in five years, not to be joinable."
        ),
    )
    reason = models.TextField(
        help_text=(
            "Why. **Required on every action**, because a credit grant with no "
            "explanation is indistinguishable from a bug six weeks later."
        ),
    )
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="What was requested — the amount, the days, the flags.",
    )
    outcome = models.CharField(
        max_length=16,
        choices=Outcome.choices,
        default=Outcome.SUCCESS,
        db_index=True,
        help_text=(
            "Failures are recorded too. An operator repeatedly failing to do "
            "something is a signal, and it disappears if only successes are kept."
        ),
    )
    result = models.JSONField(
        default=dict,
        blank=True,
        help_text="What happened — the new balance, the new end date, the error.",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)

    objects = OperatorAuditLogManager()
    all_objects = OperatorAuditLogQuerySet.as_manager()

    class Meta:
        db_table = "user_operator_audit_log"
        verbose_name = "Operator Audit Log"
        verbose_name_plural = "Operator Audit Log"
        ordering = ["-created_at"]
        default_manager_name = "objects"
        indexes = [
            models.Index(fields=["tenant", "-created_at"]),
            models.Index(fields=["actor", "-created_at"]),
        ]

    # --- append-only enforcement --------------------------------------------
    # Historical models in migrations do not carry these overrides, so a RunPython
    # data migration can still create rows normally.

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise NotImplementedError(_IMMUTABLE)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise NotImplementedError(_IMMUTABLE)

    def soft_delete(self, user=None):
        raise NotImplementedError(_IMMUTABLE)

    def __str__(self):
        who = self.tenant.name if self.tenant_id else "platform"
        return f"{self.actor_id} {self.action} on {who}"
