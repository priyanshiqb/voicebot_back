"""Move every existing subscription's grant onto the subscription itself.

``allocated_credits`` is becoming a fact about a *sale* rather than about a catalogue
row, because once the grant depends on a quantity of minutes a single number on the
plan cannot answer it.

**This has to run now, not later.** The value comes from ``Plan.allocated_credits``
for any subscription whose snapshot predates the key, and that column is scheduled to
be dropped. Reading it while it is still here is what turns the drop into a one-line
migration instead of a risk -- afterwards there would be nowhere left to read from,
and ``Subscription.rules`` would have no answer for a live subscription.

**Snapshots are not touched.** They are the frozen contract a tenant agreed to; this
reads them and writes a column beside them. Rewriting a snapshot would change what
somebody bought.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Subscription = apps.get_model("onboard", "Subscription")

    updated = []
    queryset = Subscription.objects.select_related("plan").only(
        "id", "allocated_credits", "plan_snapshot", "plan__allocated_credits"
    )

    for subscription in queryset.iterator(chunk_size=500):
        if subscription.allocated_credits is not None:
            continue

        snapshot = subscription.plan_snapshot or {}
        granted = snapshot.get("allocated_credits")

        if granted is None and subscription.plan_id:
            # The snapshot predates the key. The plan column is the only record of
            # what this cycle granted, and it is still here -- which is the whole
            # reason this migration runs at this point in the sequence.
            granted = subscription.plan.allocated_credits

        subscription.allocated_credits = int(granted or 0)
        updated.append(subscription)

        if len(updated) >= 500:
            Subscription.objects.bulk_update(updated, ["allocated_credits"])
            updated = []

    if updated:
        Subscription.objects.bulk_update(updated, ["allocated_credits"])


def unbackfill(apps, schema_editor):
    """Clear the column. Reversible because it only ever copied what was already
    recorded elsewhere -- nothing is lost by putting it back."""
    Subscription = apps.get_model("onboard", "Subscription")
    Subscription.objects.update(allocated_credits=None)


class Migration(migrations.Migration):

    dependencies = [
        ("onboard", "0030_plan_credit_basis_plan_max_minutes_plan_min_minutes_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
