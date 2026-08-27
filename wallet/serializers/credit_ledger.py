from rest_framework import serializers

from utils.serializers.base_model_core_serializer import BaseModelCoreSerializer
from wallet.models.credit_ledger import CreditLedger


class CreditLedgerRatingSerializer(serializers.Serializer):
    """The arithmetic behind a usage charge, nested under ``rating``.

    Grouped rather than flattened because these five fields only mean anything
    together: "90 seconds, per minute, 2 units, 5 credits each" is one statement, and
    five sibling nulls on a non-usage row is noise.
    """

    raw_quantity = serializers.IntegerField(read_only=True)
    units = serializers.IntegerField(read_only=True)
    unit_type = serializers.CharField(read_only=True)
    rate_applied = serializers.IntegerField(read_only=True)


class CreditLedgerRefSerializer(serializers.ModelSerializer):
    """Lightweight — for nesting beside a balance."""

    class Meta:
        model = CreditLedger
        fields = ("id", "entry_type", "amount", "balance_after", "created_at")
        read_only_fields = fields


class CreditLedgerSerializer(BaseModelCoreSerializer):
    """Full representation. Read-only throughout: the ledger is append-only, so there
    is no write path for this serializer to support."""

    entry_type_display = serializers.CharField(source="get_entry_type_display", read_only=True)
    bucket_display = serializers.CharField(source="get_bucket_display", read_only=True)
    rating = serializers.SerializerMethodField()

    class Meta(BaseModelCoreSerializer.Meta):
        model = CreditLedger
        fields = (
            "id",
            "entry_type",
            "entry_type_display",
            "bucket",
            "bucket_display",
            "amount",
            "balance_after",
            "operation",
            "rating",
            "reference_id",
            "notes",
            "created_at",
        )
        read_only_fields = fields

    def get_rating(self, obj) -> dict:
        """Present on usage rows, ``None`` elsewhere.

        This is what answers "why was I charged 12 credits?" without a support ticket
        — and it stays correct after a price change, because the numbers were copied
        onto the row rather than looked up now.
        """
        if obj.units is None:
            return None
        return CreditLedgerRatingSerializer(obj).data


class CreditLedgerListSerializer(CreditLedgerSerializer):
    """Same shape as the detail serializer.

    A ledger row *is* its own summary — trimming fields here would only mean the list
    could not answer the question the list exists to answer.
    """
