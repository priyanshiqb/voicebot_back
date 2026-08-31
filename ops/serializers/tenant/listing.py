"""One row of the tenant list.

Every field here is read off an annotation from ``ops.services.tenant.listing``, not
off a relation. A ``SerializerMethodField`` that touched ``obj.subscriptions`` would
reintroduce the per-row query the annotations exist to remove, and it would do it
invisibly — the list would still be correct, just five times more expensive per row
every month.
"""

from rest_framework import serializers


class TenantListSerializer(serializers.Serializer):
    """Read-only. The list never writes."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    subdomain = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    is_deleted = serializers.BooleanField(read_only=True)
    deleted_at = serializers.DateTimeField(read_only=True)

    plan = serializers.SerializerMethodField()
    plan_code = serializers.SerializerMethodField()
    subscription_status = serializers.CharField(read_only=True, allow_null=True)
    cycle_ends = serializers.DateTimeField(read_only=True, allow_null=True)
    has_scheduled_change = serializers.BooleanField(read_only=True)

    balance = serializers.SerializerMethodField()
    wallet_frozen = serializers.SerializerMethodField()

    credits_enforced = serializers.SerializerMethodField()
    entitlements_enforced = serializers.SerializerMethodField()

    last_call_at = serializers.DateTimeField(read_only=True, allow_null=True)

    def get_plan(self, obj):
        """The snapshot's name, falling back to the live plan.

        The snapshot is what the tenant bought. Rows written before snapshots existed
        have none, and for those the live plan is the only answer there is.
        """
        return getattr(obj, "plan_name", None) or getattr(obj, "live_plan_name", None)

    def get_plan_code(self, obj):
        return getattr(obj, "plan_code", None) or getattr(obj, "live_plan_code", None)

    def get_balance(self, obj):
        """A tenant with no wallet row has no credits, not an unknown number."""
        return getattr(obj, "balance", None) or 0

    def get_wallet_frozen(self, obj):
        return bool(getattr(obj, "wallet_frozen", None))

    def get_credits_enforced(self, obj):
        # NULL means no settings row, which means the model's default — off. Serving
        # `null` here would make the console render a third state that does not exist.
        return bool(getattr(obj, "credits_enforced", None))

    def get_entitlements_enforced(self, obj):
        return bool(getattr(obj, "entitlements_enforced", None))
