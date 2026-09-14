from django import forms
from django.contrib import admin
from django.contrib.admin.helpers import ActionForm
from django.utils import timezone

from django.forms.models import BaseInlineFormSet

from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from wallet.models.credit_ledger import CreditLedger
from wallet.models.wallet import Wallet

#: How many missed calls one admin action will bill in a single request. A long
#: backlog belongs in a management command — a request that times out halfway
#: leaves an operator guessing how much of it landed.
MAX_RETRO_CHARGES = 50


class CreditLedgerFormSet(BaseInlineFormSet):
    def get_queryset(self):
        qs = super().get_queryset()
        return qs[:20]


class CreditLedgerInline(admin.TabularInline):
    """The last 20 movements, in place. Read-only, like the ledger itself.

    A balance without its history is unactionable — the first question anyone opens a
    wallet to ask is "why is it that number", and this answers it without a second
    page and a filter.
    """

    model = CreditLedger
    formset = CreditLedgerFormSet
    extra = 0
    max_num = 0
    can_delete = False
    fields = ("created_at", "entry_type", "bucket", "amount", "balance_after", "reference_id")
    readonly_fields = fields
    ordering = ("-created_at",)
    verbose_name_plural = "Recent movements"

    def has_add_permission(self, request, obj=None):
        return False


class WalletAdminForm(forms.ModelForm):
    """Validates a hand-edited rate card before it can price a call.

    ``rate_snapshot`` is what every call is billed against, and a raw JSONField will
    accept any well-formed JSON — including a card that is missing `unit`, spells an
    operation wrongly, or carries a string where a number belongs. None of those fail
    at save time; they fail at call time, on a live tenant, as either a crash or a
    silently wrong charge.

    So the shape is checked here. This is not a substitute for knowing what the
    numbers mean — it cannot tell you 3 credits a minute is the right price — but it
    guarantees that whatever is saved is something `resolve_rate` can read.
    """

    class Meta:
        model = Wallet
        fields = "__all__"

    def clean_rate_snapshot(self):
        from wallet.models.credit_rate import CreditRate

        snapshot = self.cleaned_data.get("rate_snapshot")
        if snapshot in (None, "", {}):
            # Allowed: an empty card means "fall back to the live plan", which is a
            # legitimate state and the one a wallet starts in.
            return snapshot

        if not isinstance(snapshot, dict):
            raise forms.ValidationError("Must be a JSON object.")

        operations = snapshot.get("operations")
        if not isinstance(operations, dict):
            raise forms.ValidationError(
                'Missing an "operations" object. Rating reads '
                'rate_snapshot["operations"][<operation>] and nothing else.'
            )

        valid_operations = set(CreditRate.Operation.values)
        valid_units = set(CreditRate.Unit.values)
        valid_rounding = set(CreditRate.Rounding.values)

        for name, entry in operations.items():
            if name not in valid_operations:
                raise forms.ValidationError(
                    f'Unknown operation "{name}". One of: '
                    f"{', '.join(sorted(valid_operations))}."
                )
            if not isinstance(entry, dict):
                raise forms.ValidationError(f'"{name}" must be a JSON object.')

            unit = entry.get("unit")
            if unit not in valid_units:
                raise forms.ValidationError(
                    f'"{name}".unit must be one of: {", ".join(sorted(valid_units))}.'
                )

            credits_per_unit = entry.get("credits_per_unit")
            # Bool first: `isinstance(True, int)` is True in Python, and `true` here
            # would sail through as 1 credit.
            if isinstance(credits_per_unit, bool) or not isinstance(
                credits_per_unit, int
            ):
                raise forms.ValidationError(
                    f'"{name}".credits_per_unit must be a whole number, not '
                    f"{type(credits_per_unit).__name__}."
                )
            if credits_per_unit < 0:
                raise forms.ValidationError(
                    f'"{name}".credits_per_unit cannot be negative.'
                )

            rounding = entry.get("rounding", CreditRate.Rounding.CEIL)
            if rounding not in valid_rounding:
                raise forms.ValidationError(
                    f'"{name}".rounding must be one of: '
                    f"{', '.join(sorted(valid_rounding))}."
                )

            minimum_units = entry.get("minimum_units", 1)
            if isinstance(minimum_units, bool) or not isinstance(minimum_units, int):
                raise forms.ValidationError(
                    f'"{name}".minimum_units must be a whole number.'
                )
            if minimum_units < 0:
                raise forms.ValidationError(
                    f'"{name}".minimum_units cannot be negative.'
                )

        return snapshot


class CreditAdjustmentForm(ActionForm):
    """Extra fields on the action bar, so a grant needs an amount and a reason.

    An ActionForm rather than an intermediate page: it keeps the whole interaction on
    one screen, and there is nothing here worth a confirmation step that a required
    reason field does not already provide.
    """

    adjustment_amount = forms.IntegerField(
        required=False, min_value=1, label="Credits",
        widget=forms.NumberInput(attrs={"placeholder": "e.g. 500", "style": "width:8em"}),
    )
    adjustment_reason = forms.CharField(
        required=False, label="Reason / ticket",
        widget=forms.TextInput(attrs={"placeholder": "TICKET-123 — goodwill", "style": "width:22em"}),
    )


@admin.register(Wallet)
class WalletAdmin(TenantBaseAdminCore):
    """
    A tenant's credit balance.

    **The balance fields are read-only, deliberately.** They are a cache of the
    ledger, and editing one here would break the invariant silently — the number
    would change while the history that explains it did not. Credits move by posting
    a ledger entry, never by typing a new total.

    **Grant and Deduct go through ``wallet.services.wallet.adjust()``**, never through
    a field. The service takes the row lock, writes the ledger entry and keeps the two
    buckets consistent in one transaction; doing any of that here would make the admin
    a second writer, which is the one thing the design forbids. Both require a reason —
    an unexplained balance change is worse than none.

    Freeze and unfreeze set a flag, touch no balance and write no ledger row, so they
    stay local.
    """

    model = Wallet
    form = WalletAdminForm
    inlines = [CreditLedgerInline]

    list_display = (
        "tenant",
        "balance",
        "allocated_balance",
        "purchased_balance",
        "is_frozen",
        "current_plan",
        "low_balance_display",
        "updated_at",
    )
    list_filter = ("is_frozen",) + TenantBaseAdminCore.list_filter
    search_fields = (
        "tenant__name",
        "tenant__subdomain",
    ) + TenantBaseAdminCore.search_fields
    list_select_related = ("tenant", "current_plan")
    ordering = ("-balance",)

    readonly_fields = TenantBaseAdminCore.readonly_fields + (
        "balance",
        "allocated_balance",
        "purchased_balance",
        "lifetime_granted",
        "lifetime_consumed",
        "is_frozen",
        "frozen_reason",
        "frozen_at",
        "rate_snapshot_at",
    )

    action_form = CreditAdjustmentForm
    actions = (
        "grant_credits",
        "deduct_credits",
        "freeze_wallets",
        "unfreeze_wallets",
        "check_reconciliation",
        "charge_unrated_calls",
        "fix_reconciliation",
        "show_unrated_calls",
    )

    fieldsets = (
        ("Balance", {
            "fields": ("id", "tenant", "balance", "allocated_balance", "purchased_balance"),
            "description": (
                "Read-only. Allocated is what a plan cycle granted and what rollover "
                "rules can burn; purchased was bought with cash and always survives. "
                "Consumption drains allocated first."
            ),
        }),
        ("Spendability", {"fields": ("is_frozen", "frozen_reason", "frozen_at")}),
        ("Rate card in force", {
            "fields": ("current_plan", "rate_snapshot", "rate_snapshot_at"),
            "description": (
                "Stamped from the plan when a subscription activates, renews or "
                "changes. This — not the CreditRate table — is what prices a call. "
                "<br><br>"
                "<strong>Set `current_plan` if it is empty.</strong> Rating falls back "
                "to that plan's live CreditRate rows for any operation missing from "
                "the card below — so with it set, adding a rate to the plan takes "
                "effect immediately. With it empty there is no fallback at all, and "
                "any tier absent from the card fails the charge outright. A wallet "
                "reaches that state when stamping failed during onboarding, which also "
                "means the cycle credits were never granted — check the balance too. "
                "<br><br>"
                "<strong>Editable, and it takes effect on the next call.</strong> "
                "Shape: <code>{\"operations\": {\"conversation_low_latency\": "
                "{\"unit\": \"per_minute\", \"credits_per_unit\": 3, "
                "\"rounding\": \"ceil\", \"minimum_units\": 1}}}</code>. "
                "Operations: static_reel, conversation_high_latency, "
                "conversation_low_latency. Units: per_event, per_second, per_minute. "
                "Rounding: ceil, floor, nearest."
                "<br><br>"
                "<strong>An edit here is temporary.</strong> Renewal, a plan change "
                "and re-activation all call set_plan_context, which rebuilds this "
                "from the plan and discards anything typed here. For a rate that has "
                "to survive a renewal, change the plan's CreditRate — or ask for the "
                "per-wallet override to be built."
                "<br><br>"
                "An operation left out of this card is not free: rating falls through "
                "to the live plan for it, and errors only if the plan has no rate "
                "either."
            ),
        }),
        ("Alerting", {"fields": ("low_balance_threshold", "low_balance_notified_at")}),
        ("Lifetime", {
            "fields": ("lifetime_granted", "lifetime_consumed"),
            "classes": ("collapse",),
        }),
        ("Timestamps", {"fields": ("created_at", "updated_at", "deleted_at")}),
    )

    @admin.display(description="Low balance", boolean=True)
    def low_balance_display(self, obj):
        return obj.is_low

    @admin.action(description="Freeze — block spending")
    def freeze_wallets(self, request, queryset):
        updated = queryset.filter(is_frozen=False).update(
            is_frozen=True,
            frozen_reason=f"admin:{request.user}",
            frozen_at=timezone.now(),
        )
        self.message_user(request, f"Froze {updated} wallet(s). Grants still land; spending is blocked.")

    @admin.action(description="Unfreeze — allow spending")
    def unfreeze_wallets(self, request, queryset):
        updated = queryset.filter(is_frozen=True).update(
            is_frozen=False, frozen_reason=None, frozen_at=None
        )
        self.message_user(request, f"Unfroze {updated} wallet(s).")

    # --- credit movement, through the service --------------------------------

    def _adjustment_args(self, request):
        """The amount and reason from the action bar, or an error string."""
        raw = (request.POST.get("adjustment_amount") or "").strip()
        reason = (request.POST.get("adjustment_reason") or "").strip()
        if not raw:
            return None, None, "Enter a number of credits."
        if not reason:
            return None, None, "Enter a reason — an unexplained balance change is worse than none."
        try:
            amount = int(raw)
        except ValueError:
            return None, None, f"'{raw}' is not a whole number of credits."
        if amount < 1:
            return None, None, "Credits must be a positive whole number."
        return amount, reason, None

    def _apply_adjustment(self, request, queryset, sign):
        """Move credits via wallet.services.wallet.adjust().

        Never touches a balance directly. The service takes the row lock, writes the
        ledger entry and keeps the buckets consistent — doing any of that here would
        make the admin a second writer, which is exactly what the design forbids.
        """
        from wallet.services import wallet as svc

        amount, reason, error = self._adjustment_args(request)
        if error:
            self.message_user(request, error, level="ERROR")
            return

        # Second-precision timestamp: a double-submitted form inside the same second
        # is absorbed by the ledger's idempotency constraint, while a deliberate
        # repeat a minute later still works.
        stamp = timezone.now().strftime("%Y%m%d%H%M%S")
        moved = blocked = 0

        for wallet in queryset:
            try:
                entry = svc.adjust(
                    wallet.tenant_id,
                    amount=sign * amount,
                    reference_id=f"admin:{wallet.pk}:{stamp}",
                    performed_by=request.user,
                    notes=reason,
                )
            except Exception as exc:  # noqa: BLE001 — surfaced to the operator below
                self.message_user(request, f"{wallet.tenant}: {exc}", level="ERROR")
                blocked += 1
                continue
            if entry is None:
                blocked += 1
            else:
                moved += 1

        verb = "Granted" if sign > 0 else "Deducted"
        message = f"{verb} {amount} credit(s) on {moved} wallet(s)."
        if blocked:
            message += f" {blocked} skipped — already applied, or nothing left to deduct."
        self.message_user(request, message)

    @admin.action(description="Grant credits (enter amount + reason above)")
    def grant_credits(self, request, queryset):
        self._apply_adjustment(request, queryset, sign=1)

    @admin.action(description="Deduct credits (enter amount + reason above)")
    def deduct_credits(self, request, queryset):
        self._apply_adjustment(request, queryset, sign=-1)

    # --- Health -----------------------------------------------------------
    # All three call the same services as the nightly jobs and the ops console —
    # `wallet.services.reconciliation` and `unrated_for`. A fourth copy of either
    # comparison would let this screen disagree with the job about whether a balance
    # adds up, and no operator would know which to believe.

    @admin.action(description="Check reconciliation — does the balance match its ledger?")
    def check_reconciliation(self, request, queryset):
        from wallet.services import reconciliation

        clean = 0
        for wallet in queryset:
            problems = reconciliation.check(wallet)
            if not problems:
                clean += 1
                continue
            for problem in problems:
                self.message_user(
                    request,
                    f"{wallet.tenant}: {problem['message']}",
                    level="WARNING",
                )
        if clean:
            self.message_user(
                request, f"{clean} wallet(s) reconcile against their ledger."
            )

    @admin.action(description="Fix reconciliation drift (enter reason above)")
    def fix_reconciliation(self, request, queryset):
        """Correct drift by *posting* the difference, never by editing the balance.

        Same contract as ``reconcile_wallets --fix``: the correction is an
        ADMIN_ADJUSTMENT ledger entry, so the repair is as auditable as whatever
        caused it. A reason is required for the same reason a grant needs one.
        """
        from wallet.services import reconciliation
        from wallet.services import wallet as svc

        reason = (request.POST.get("adjustment_reason") or "").strip()
        if not reason:
            self.message_user(
                request,
                "Enter a reason — drift is a bug until somebody explains it, and a "
                "silent correction destroys the evidence.",
                level="ERROR",
            )
            return

        stamp = timezone.now().strftime("%Y%m%d%H%M%S")
        fixed = 0

        for wallet in queryset:
            if not reconciliation.check(wallet):
                continue
            total = reconciliation.ledger_total(wallet)
            delta = total - wallet.balance
            if delta == 0:
                continue
            try:
                svc.adjust(
                    wallet.tenant_id,
                    amount=delta,
                    reference_id=f"reconcile:{wallet.pk}:{stamp}",
                    performed_by=request.user,
                    notes=f"Reconciliation. {reason}".strip(),
                )
            except Exception as exc:  # noqa: BLE001 — surfaced to the operator
                self.message_user(request, f"{wallet.tenant}: {exc}", level="ERROR")
                continue
            fixed += 1
            self.message_user(
                request, f"{wallet.tenant}: adjusted by {delta:+} to match the ledger."
            )

        if not fixed:
            self.message_user(request, "Nothing to fix — every wallet reconciles.")

    @admin.action(description="Charge unrated calls (enter reason above)")
    def charge_unrated_calls(self, request, queryset):
        """Bill calls that completed but were never charged.

        The nightly sweep only *reports* these, for a stated reason: a retroactive
        charge bills at whatever rate is stamped now, which is not necessarily the
        rate that applied when the call happened. That makes it an operator decision,
        not an automatic one — which is why this is a deliberate action behind a
        required reason rather than a job.

        Goes through ``wallet.services.wallet.consume()``, the same function the call
        log's own charge hook uses, keyed on ``provider_call_id``. That matters: the
        entry lands as a USAGE_DEDUCTION against the call's own reference, so the call
        stops being reported as unrated. A manual Deduct would take the credits and
        leave it listed forever, with nothing to distinguish it from a call that was
        never charged at all.

        Safe to re-run — ``consume`` ignores a reference it has already recorded.
        """
        from core.models.agent_call_log import AgentCallLog
        from wallet.services import policy
        from wallet.services import wallet as svc
        from wallet.services.exceptions import (
            InsufficientCredits,
            RateNotConfigured,
            WalletFrozen,
        )
        from wallet.tasks.unrated_calls import DEFAULT_WINDOW_HOURS, unrated_for

        reason = (request.POST.get("adjustment_reason") or "").strip()
        if not reason:
            self.message_user(
                request,
                "Enter a reason — billing days after the fact is a judgement call, "
                "and the ledger should say whose.",
                level="ERROR",
            )
            return

        charged = skipped = failed = 0

        for wallet in queryset:
            rows = unrated_for(wallet.tenant_id, hours=DEFAULT_WINDOW_HOURS)
            if not rows:
                continue

            # Bounded: an admin request is not the place to work through a long
            # backlog, and a half-finished batch that timed out is worse than one
            # that says how much is left.
            if len(rows) > MAX_RETRO_CHARGES:
                self.message_user(
                    request,
                    f"{wallet.tenant}: {len(rows)} unrated calls — too many to charge "
                    f"from here ({MAX_RETRO_CHARGES} at a time). Run it again, or use "
                    "a management command for a backlog this size.",
                    level="WARNING",
                )
                rows = rows[:MAX_RETRO_CHARGES]

            for provider_call_id, _tenant_id, _duration, _created_at in rows:
                log = AgentCallLog.objects.filter(
                    provider_call_id=provider_call_id, tenant_id=wallet.tenant_id
                ).first()
                if log is None:
                    skipped += 1
                    continue

                billable = policy.operation_for_call(log)
                if billable is None:
                    skipped += 1
                    continue

                operation, quantity = billable
                try:
                    result = svc.consume_detailed(
                        wallet.tenant_id,
                        operation=operation,
                        quantity=quantity,
                        reference_id=log.provider_call_id,
                        metadata={
                            "agent_id": str(log.agent_id),
                            "direction": log.direction,
                            # Marks this as a retro-charge rather than a call-time one,
                            # so a later audit can tell them apart.
                            "charged_retroactively": True,
                            "reason": reason,
                        },
                    )
                except RateNotConfigured as exc:
                    failed += 1
                    self.message_user(
                        request,
                        f"{wallet.tenant}: {exc} — set `current_plan` or the rate card "
                        "on this wallet first.",
                        level="ERROR",
                    )
                    break
                except (InsufficientCredits, WalletFrozen) as exc:
                    failed += 1
                    self.message_user(
                        request, f"{wallet.tenant}: {exc}", level="ERROR"
                    )
                    break
                except Exception as exc:  # noqa: BLE001 — surfaced to the operator
                    failed += 1
                    self.message_user(
                        request,
                        f"{wallet.tenant}: {provider_call_id}: {exc}",
                        level="ERROR",
                    )
                    continue

                # None means already charged, or the wallet could not cover it.
                if result is None:
                    skipped += 1
                    continue

                # A free call moved nothing, so it is not "charged", but it was rated —
                # recording its 0 below is what stops it reading as never billed.
                if result.entries:
                    charged += 1
                else:
                    skipped += 1

                # Recorded exactly as a call-time charge is, or a retro-charged call sits
                # in every report as "never charged". Surfaced rather than swallowed:
                # the credits have moved, and the operator should know the log does not
                # say so yet.
                try:
                    log.record_credits_charged(result.credits)
                except Exception as exc:  # noqa: BLE001 — surfaced to the operator
                    self.message_user(
                        request,
                        f"{wallet.tenant}: {provider_call_id} was charged "
                        f"{result.credits} credits, but its call log could not record "
                        f"it: {exc}",
                        level="WARNING",
                    )

        if not (charged or skipped or failed):
            self.message_user(
                request, "Nothing to charge — no unrated calls on the selected wallets."
            )
            return

        message = f"Charged {charged} call(s)."
        if skipped:
            message += f" {skipped} skipped — already charged, or nothing to rate."
        if failed:
            message += f" {failed} failed; see the errors above."
        self.message_user(request, message)

    @admin.action(description="Show unrated calls (completed but never charged)")
    def show_unrated_calls(self, request, queryset):
        """Calls that produced no deduction — uncollected revenue.

        Reports only. Charging retroactively would bill at whatever rate is stamped
        now, which is not the rate that applied when the call happened.

        Note what this cannot catch: a call charged at the *wrong* rate is rated,
        reconciles, and never appears here. A missing rate shows up; a mistyped one
        does not.
        """
        from wallet.tasks.unrated_calls import DEFAULT_WINDOW_HOURS, unrated_for

        total = 0
        for wallet in queryset:
            rows = unrated_for(wallet.tenant_id, hours=DEFAULT_WINDOW_HOURS)
            if not rows:
                continue
            total += len(rows)
            self.message_user(
                request,
                f"{wallet.tenant}: {len(rows)} unrated call(s) in the last "
                f"{DEFAULT_WINDOW_HOURS}h — "
                + ", ".join(f"{call_id} ({duration}s)"
                            for call_id, _t, duration, _c in rows[:5])
                + (" …" if len(rows) > 5 else ""),
                level="WARNING",
            )
        if not total:
            self.message_user(
                request,
                f"No unrated calls in the last {DEFAULT_WINDOW_HOURS}h. "
                "Note that calls under 15 minutes old are still within the grace "
                "window and are not counted.",
            )
