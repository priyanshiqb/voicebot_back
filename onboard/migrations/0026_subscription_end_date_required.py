from dateutil.relativedelta import relativedelta
from django.db import migrations, models
from django.utils import timezone


def backfill_any_stragglers(apps, schema_editor):
    """Fill any end_date still missing before the column stops allowing NULL.

    Migration 0025 already backfilled every row, so this normally touches nothing.
    It exists because the window between the two migrations is real: an environment
    that ran 0025, then created subscriptions with older code, would fail the
    AlterField below with a constraint violation and no explanation.
    """
    Subscription = apps.get_model("onboard", "Subscription")
    Plan = apps.get_model("onboard", "Plan")

    deltas = {"monthly": relativedelta(months=1), "yearly": relativedelta(years=1)}
    plans = {p.pk: p for p in Plan.objects.all()}
    filled = 0

    for sub in Subscription.objects.filter(end_date__isnull=True):
        plan = plans.get(sub.plan_id)
        cycle = plan.billing_cycle if plan else "monthly"
        start = sub.start_date or sub.created_at or timezone.now()
        sub.end_date = start + deltas.get(cycle, deltas["monthly"])
        sub.save(update_fields=["end_date"])
        filled += 1

    if filled:
        print(f"    filled end_date on {filled} straggler subscription(s)")


def allow_null_again(apps, schema_editor):
    """Reverse is a no-op: the data stays valid either way."""


class Migration(migrations.Migration):

    dependencies = [
        ("onboard", "0025_subscription_canceled_at_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_any_stragglers, allow_null_again),
        migrations.AlterField(
            model_name="subscription",
            name="end_date",
            field=models.DateTimeField(
                help_text=(
                    "When this billing cycle ends. Derived from start_date plus the "
                    "plan's billing cycle when not set explicitly. Non-nullable, "
                    "deliberately: expiry, renewal windows and proration are all "
                    "functions of this, so a subscription without one has no billing "
                    "cycle to reason about — and every row in this table was in "
                    "exactly that state until it was backfilled. save() fills it when "
                    "unset, so the database constraint is a backstop rather than a "
                    "burden on callers."
                )
            ),
        ),
    ]
