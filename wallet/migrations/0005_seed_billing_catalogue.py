from django.db import migrations


class Migration(migrations.Migration):
    """Intentionally empty.

    This migration once seeded plan credit rules, entitlements and rate cards from a
    hard-coded module. That module is gone: the admin is the single source of truth
    for what a plan grants and charges, and a second definition in Python could only
    disagree with it.

    The rows it created are still in the database and are now maintained through the
    admin — a plan's rate card and entitlements are both editable inline on its page.
    The file remains so that migration history stays consistent for databases where it
    already ran; there is nothing to re-apply and nothing to reverse.
    """

    dependencies = [
        ("wallet", "0004_backfill_tenant_wallets"),
        ("onboard", "0025_subscription_canceled_at_and_more"),
    ]

    operations = []
