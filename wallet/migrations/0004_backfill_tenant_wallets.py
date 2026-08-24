from django.db import migrations


def create_wallets_for_existing_tenants(apps, schema_editor):
    """Give every live tenant a wallet.

    ``Wallet.for_tenant()`` creates one on demand, so this is not strictly required —
    but a tenant whose wallet springs into existence on their first call has no row
    for an admin to look at, no low-balance threshold, and nothing for the
    subscription engine to stamp a rate card onto. Creating them up front means every
    tenant is visible in the wallet admin from the moment this lands.

    **No ledger row is written.** The plan originally called for a zero
    ``OPENING_BALANCE`` entry per wallet, which cannot exist: ``ck_ledger_amount_non_zero``
    rejects an amount of 0, and rightly so — a zero-credit movement is noise in an
    audit trail. It is also unnecessary. The invariant is
    ``SUM(ledger) == balance``, and an empty ledger sums to zero, which is exactly the
    balance being opened. ``OPENING_BALANCE`` stays in the enum for the case it is
    actually for: opening a wallet at a *non-zero* balance, such as a migration from
    another system or a goodwill grant at creation.

    Idempotent — skips tenants that already have a wallet, so it is safe to re-run
    against a database where some exist.
    """
    Tenant = apps.get_model("tenant", "Tenant")
    Wallet = apps.get_model("wallet", "Wallet")

    created = 0
    for tenant_id in Tenant.objects.filter(is_deleted=False).values_list("id", flat=True):
        # Historical models carry a plain manager, so soft-deleted rows are visible
        # here; matching on is_deleted keeps this consistent with the partial unique
        # constraint, which also ignores them.
        _, was_created = Wallet.objects.get_or_create(
            tenant_id=tenant_id,
            is_deleted=False,
            defaults={
                "balance": 0,
                "allocated_balance": 0,
                "purchased_balance": 0,
            },
        )
        created += int(was_created)

    print(f"    created {created} wallet(s)")


def delete_empty_wallets(apps, schema_editor):
    """Reverse: remove wallets that never held anything.

    A wallet with a balance or any ledger history is left alone — reversing a
    migration must not destroy a financial record.
    """
    Wallet = apps.get_model("wallet", "Wallet")
    removed = (
        Wallet.objects.filter(balance=0, entries__isnull=True).delete()
    )
    print(f"    removed {removed}")


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0003_alter_creditrate_operation_alter_creditrate_unit"),
        ("tenant", "0009_tenantsettings_credits_enforced"),
    ]

    operations = [
        migrations.RunPython(create_wallets_for_existing_tenants, delete_empty_wallets),
    ]
