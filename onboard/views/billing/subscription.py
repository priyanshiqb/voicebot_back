import logging

from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status

from onboard.models import Plan, Transaction
from onboard.serializers.billing import (
    CurrentSubscriptionSerializer,
    EligiblePlansSerializer,
    OrderCreatedSerializer,
    PlanChangeAppliedSerializer,
    PlanChangePreviewSerializer,
    PlanChangeSerializer,
    ScheduledChangeCancelledSerializer,
)
from onboard.services import entitlements as entitlement_service
from onboard.services import payments, pricing
from onboard.services import subscriptions as subscription_service
from onboard.views.billing.purchases import _PurchaseBase

logger = logging.getLogger(__name__)

TxnType = Transaction.TransactionTypeChoices

#: How early a tenant may renew. Long enough to act on a reminder, short enough that
#: nobody pays twice for a cycle they are barely into.
RENEWAL_WINDOW_DAYS = 14


@extend_schema(
    tags=["Billing - Subscription"],
    summary="Current subscription",
    description=(
        "The live plan, its cycle, purchased capacity and credit balance. Returned "
        "for a lapsed subscription too, so an expired tenant can see what they had "
        "and what it costs to come back."
    ),
    responses={200: CurrentSubscriptionSerializer},
)
class CurrentSubscriptionView(_PurchaseBase):
    serializer_class = CurrentSubscriptionSerializer
    def get(self, request):
        tenant = self.get_tenant()
        subscription, error = self._subscription_or_expired_or_error()
        if error:
            return error

        from wallet.services import wallet as wallet_service

        now = timezone.now()
        days_left = max((subscription.end_date - now).days, 0)

        return self.success(
            data={
                "id": str(subscription.pk),
                "status": subscription.status,
                "plan": {
                    "id": str(subscription.plan_id),
                    "code": subscription.plan.code,
                    "name": subscription.plan.name,
                    "billing_cycle": subscription.plan.billing_cycle,
                },
                "selected_minutes": subscription.selected_minutes,
                "allocated_credits": subscription.allocated_credits,
                "start_date": subscription.start_date,
                "end_date": subscription.end_date,
                "days_remaining": days_left,
                "renewable_now": days_left <= RENEWAL_WINDOW_DAYS,
                "scheduled_change": (
                    {
                        "plan_id": str(subscription.scheduled_plan_id),
                        "plan_code": subscription.scheduled_plan.code,
                        "selected_minutes": subscription.scheduled_minutes,
                        "effective_at": subscription.scheduled_change_at,
                    }
                    if subscription.scheduled_plan_id
                    else None
                ),
                "credits": wallet_service.balance_for(tenant),
                "entitlements": entitlement_service.summary(
                    tenant.id, subscription, with_usage=True
                ),
                "addons_recurring_net": str(
                    entitlement_service.recurring_total(tenant.id)
                ),
            },
            message="Subscription retrieved",
        )


@extend_schema(
    tags=["Billing - Subscription"],
    summary="Plans this tenant can move to",
    description=(
        "Every active plan, each tagged with what moving to it would mean and what it "
        "would cost. Saves the pricing page calling preview-change once per plan."
    ),
    responses={200: EligiblePlansSerializer},
)
class EligiblePlansView(_PurchaseBase):
    serializer_class = EligiblePlansSerializer
    def get(self, request):
        subscription, error = self._subscription_or_expired_or_error()
        if error:
            return error

        # Optional: price every plan at a quantity the tenant is considering rather
        # than at the one they hold. Without it the table answers "what does each plan
        # cost at some quantity nobody asked about".
        try:
            requested_minutes = int(request.query_params.get("minutes") or 0) or None
        except (TypeError, ValueError):
            requested_minutes = None

        # Pricing a plan reads its basis rate, so the whole catalogue's open rows are
        # fetched once rather than one plan at a time. QB-11 measures this.
        from django.db.models import Prefetch

        from wallet.models import CreditRate

        catalogue = (
            Plan.objects.filter(is_active=True, is_deleted=False)
            .order_by("sort_order")
            .prefetch_related(
                Prefetch(
                    "credit_rates",
                    queryset=CreditRate.objects.filter(
                        is_deleted=False, effective_to__isnull=True
                    ),
                    to_attr="open_rates",
                )
            )
        )

        plans = []
        for plan in catalogue:
            entry = {
                # `preview-change/` and `change-plan/` both take `plan_id`, so the
                # id has to be here -- otherwise picking a plan from this list means
                # a round trip to the Plans API just to translate the code.
                "id": str(plan.pk),
                "code": plan.code,
                "name": plan.name,
                "description": plan.description,
                "billing_cycle": plan.billing_cycle,
                "total_price": str(plan.total_price),
                "min_minutes": plan.min_minutes,
                "max_minutes": plan.max_minutes,
                "is_fixed_quantity": plan.is_fixed_quantity,
                "is_current": plan.pk == subscription.plan_id,
                "is_most_popular": plan.is_most_popular,
            }

            if entry["is_current"]:
                entry["change_type"] = "current"
                entry["amount_due"] = "0.00"
            else:
                try:
                    quote = pricing.quote_plan_change(
                        subscription, plan, minutes=requested_minutes
                    )
                    entry["change_type"] = quote.detail["change_type"]
                    entry["amount_due"] = str(quote.total)
                    entry["selected_minutes"] = quote.detail.get("selected_minutes")
                    entry["effective_at"] = quote.detail["effective_at"]
                except pricing.PricingError as exc:
                    # A plan that cannot be priced is still worth listing — the tenant
                    # should see it exists, with the reason it is unavailable.
                    entry["change_type"] = "unavailable"
                    entry["reason"] = str(exc)

            plans.append(entry)

        return self.success(data={"plans": plans}, message="Plans retrieved")


@extend_schema(
    tags=["Billing - Subscription"],
    summary="Renew the current plan",
    description=(
        "Creates an order for the next cycle, including any add-ons still held. "
        "Renewals are manual — nothing is charged automatically."
    ),
    request=None,
    responses={200: OrderCreatedSerializer},
)
class RenewSubscriptionView(_PurchaseBase):
    serializer_class = OrderCreatedSerializer
    def post(self, request):
        tenant = self.get_tenant()
        # Renewal is the one-click "same plan again" path and works whether the cycle
        # is running or already over.
        subscription, error = self._subscription_or_expired_or_error()
        if error:
            return error

        days_left = (subscription.end_date - timezone.now()).days
        if days_left > RENEWAL_WINDOW_DAYS:
            return self.error(
                message=(
                    f"Renewal opens {RENEWAL_WINDOW_DAYS} days before your cycle ends. "
                    f"Yours ends in {days_left} days."
                ),
                errors=["RENEWAL_WINDOW_NOT_OPEN"],
                status_code=status.HTTP_409_CONFLICT,
            )

        blocking = self._blocking_order(tenant)
        if blocking is not None:
            return self.error(
                message="An unpaid order is already open.",
                errors=["PENDING_ORDER_EXISTS"],
                data={"razorpay_order_id": blocking.razorpay_order_id},
                status_code=status.HTTP_409_CONFLICT,
            )

        plan = subscription.plan
        addons = entitlement_service.recurring_total(tenant.id)
        # The plan half priced for what this tenant actually bought, not for the
        # catalogue.
        net = pricing.net_for(plan, subscription.selected_minutes) + addons
        quote = pricing.Quote(
            net=pricing._money(net),
            tax=pricing._money(net * (plan.tax_percentage or 0) / 100),
            total=pricing._money(net + net * (plan.tax_percentage or 0) / 100),
            tax_percentage=plan.tax_percentage,
            detail={"plan_net": plan.net_base_price, "addons_net": addons},
        )

        try:
            transaction = self._create_order(
                tenant=tenant, subscription=subscription, quote=quote,
                transaction_type=TxnType.RENEWAL, receipt=f"renew_{plan.code}",
            )
        except Exception:
            return self.error(message="Could not reach the payment gateway.",
                              errors=["GATEWAY_ERROR"],
                              status_code=status.HTTP_502_BAD_GATEWAY)

        return self._order_response(transaction, quote, "Renewal order created")


@extend_schema(
    tags=["Billing - Subscription"],
    summary="Preview a plan change",
    description=(
        "What moving to another plan would cost and what it would do to your credits. "
        "Zero side effects — nothing is created, charged or scheduled."
    ),
    request=PlanChangeSerializer,
    responses={200: PlanChangePreviewSerializer},
)
class PreviewPlanChangeView(_PurchaseBase):
    serializer_class = PlanChangeSerializer

    def post(self, request):
        payload = PlanChangeSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(message="Validation failed", errors=payload.errors,
                              status_code=status.HTTP_400_BAD_REQUEST)

        subscription, error = self._subscription_or_expired_or_error()
        if error:
            return error

        plan = Plan.objects.filter(
            pk=payload.validated_data["plan_id"], is_active=True, is_deleted=False
        ).first()
        if plan is None:
            return self.error(message="Plan not found or inactive",
                              errors=["PLAN_NOT_FOUND"],
                              status_code=status.HTTP_404_NOT_FOUND)

        minutes = payload.validated_data.get("minutes")
        try:
            quote = pricing.quote_plan_change(subscription, plan, minutes=minutes)
        except pricing.PricingError as exc:
            return self.error(message=str(exc), errors=["PLAN_UNCHANGED"],
                              status_code=status.HTTP_409_CONFLICT)

        reason = (
            "upgrade" if quote.detail["change_type"] != pricing.SCHEDULED_CHANGE
            else "downgrade"
        )
        # The quantity the quote settled on, not the one that was asked for -- the
        # credit preview has to describe the same purchase the price does.
        settled = quote.detail.get("selected_minutes")
        return self.success(
            data={
                "money": quote.as_breakdown(),
                "credits": pricing.credit_impact(
                    subscription, plan, reason=reason, minutes=settled
                ),
            },
            message="Preview calculated",
        )


@extend_schema(
    tags=["Billing - Subscription"],
    summary="Change plan",
    description=(
        "Upgrades create an order and apply when it clears. Downgrades are scheduled "
        "for the end of the cycle you have already paid for — no refund, no early "
        "loss of what you bought."
    ),
    request=PlanChangeSerializer,
    responses={
        200: OpenApiResponse(
            response=PlanChangeAppliedSerializer,
            description=(
                "A downgrade scheduled, or a sub-rupee change applied outright. An "
                "upgrade returns an OrderCreated payload instead."
            ),
        ),
    },
)
class ChangePlanView(_PurchaseBase):
    serializer_class = PlanChangeSerializer

    def post(self, request):
        payload = PlanChangeSerializer(data=request.data)
        if not payload.is_valid():
            return self.error(message="Validation failed", errors=payload.errors,
                              status_code=status.HTTP_400_BAD_REQUEST)

        tenant = self.get_tenant()
        subscription, error = self._subscription_or_expired_or_error()
        if error:
            return error

        plan = Plan.objects.filter(
            pk=payload.validated_data["plan_id"], is_active=True, is_deleted=False
        ).first()
        if plan is None:
            return self.error(message="Plan not found or inactive",
                              errors=["PLAN_NOT_FOUND"],
                              status_code=status.HTTP_404_NOT_FOUND)

        minutes = payload.validated_data.get("minutes")
        settled = plan.resolve_minutes(
            minutes if minutes is not None else subscription.selected_minutes
        )

        # **The plan and the quantity together are what identifies a sale.** Buying
        # more minutes of the plan you are already on is a real change and a real
        # upgrade; only a request that moves neither is refused.
        if plan.pk == subscription.plan_id and settled == subscription.selected_minutes:
            # Coming back on exactly what you already had is a renewal, and renew/ is
            # the endpoint that extends the existing row rather than starting a new one.
            return self.error(
                message=(
                    "That is the plan you are already on. Use the renew endpoint to "
                    "continue on it."
                ),
                errors=["PLAN_UNCHANGED"],
                status_code=status.HTTP_409_CONFLICT,
            )

        try:
            # Recomputed here rather than trusting anything the client sends. A quote
            # is a display artefact; the charge is decided by the server.
            quote = pricing.quote_plan_change(subscription, plan, minutes=minutes)
        except pricing.PricingError as exc:
            return self.error(message=str(exc), errors=["PLAN_UNCHANGED"],
                              status_code=status.HTTP_409_CONFLICT)

        change_type = quote.detail["change_type"]

        if change_type == pricing.SCHEDULED_CHANGE:
            subscription_service.schedule_plan_change(
                subscription, plan, minutes=settled
            )
            return self.success(
                data={"change_type": change_type,
                      "plan_id": str(plan.pk),
                      "plan_code": plan.code,
                      "selected_minutes": settled,
                      "effective_at": subscription.end_date,
                      "charged": False},
                message=(
                    f"Downgrade to {plan.name} scheduled for "
                    f"{subscription.end_date:%d %b %Y}. Nothing changes until then."
                ),
            )

        if change_type == pricing.IMMEDIATE_FREE:
            subscription_service.apply_plan_change(
                subscription, plan, minutes=settled, reason="upgrade"
            )
            return self.success(
                data={"change_type": change_type, "plan_id": str(plan.pk),
                      "plan_code": plan.code, "selected_minutes": settled,
                      "charged": False},
                message=f"Moved to {plan.name}.",
            )

        blocking = self._blocking_order(tenant)
        if blocking is not None:
            return self.error(
                message="An unpaid order is already open.",
                errors=["PENDING_ORDER_EXISTS"],
                data={"razorpay_order_id": blocking.razorpay_order_id},
                status_code=status.HTTP_409_CONFLICT,
            )

        try:
            transaction = self._create_order(
                tenant=tenant, subscription=subscription, quote=quote,
                transaction_type=TxnType.UPGRADE,
                receipt=f"upgrade_{plan.code}", target_plan=plan,
            )
        except Exception:
            return self.error(message="Could not reach the payment gateway.",
                              errors=["GATEWAY_ERROR"],
                              status_code=status.HTTP_502_BAD_GATEWAY)

        return self._order_response(transaction, quote, "Upgrade order created")


@extend_schema(
    tags=["Billing - Subscription"],
    summary="Cancel a scheduled change",
    description="Drops a pending downgrade so the current plan simply continues.",
    request=None,
    responses={200: ScheduledChangeCancelledSerializer},
)
class CancelScheduledChangeView(_PurchaseBase):
    serializer_class = ScheduledChangeCancelledSerializer
    def delete(self, request):
        subscription, error = self._subscription_or_error()
        if error:
            return error

        if subscription.scheduled_plan_id is None:
            return self.error(message="No change is scheduled.",
                              errors=["NO_SCHEDULED_CHANGE"],
                              status_code=status.HTTP_409_CONFLICT)

        subscription_service.cancel_scheduled_change(subscription)
        return self.success(message="Scheduled change cancelled.")
