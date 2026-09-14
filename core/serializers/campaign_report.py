from rest_framework import serializers

from core.models.campaign_report import CampaignReport
from core.serializers.campaign import CampaignRefSerializer
from tenant.common.serializers.tenant_base_model_serializer import (
    TenantBaseModelSerializer,
)

#: `created_by` comes from the audited base and is the person who asked.
DETAIL_FIELDS = [
    "campaign",
    "report_format",
    "status",
    "progress_percent",
    "processed_rows",
    "total_rows",
    "rows_included",
    "file_name",
    "file_size_bytes",
    "is_downloadable",
    "started_at",
    "completed_at",
    "expires_at",
    "error_message",
]

LIST_FIELDS = [
    "campaign",
    "report_format",
    "status",
    "progress_percent",
    "file_name",
    "file_size_bytes",
    "is_downloadable",
    "completed_at",
    "expires_at",
]


class CampaignReportRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = CampaignReport
        fields = ("id", "report_format", "status")
        read_only_fields = fields


class CampaignReportSerializer(TenantBaseModelSerializer):
    """A report as the client polls it. Read-only throughout.

    **There is no download URL here, and never will be.** A link is minted per request by
    the download endpoint; serialising one would put a credential into every list response
    and every cache that stores them.
    """

    campaign = CampaignRefSerializer(read_only=True)
    progress_percent = serializers.IntegerField(read_only=True)
    is_downloadable = serializers.BooleanField(read_only=True)

    class Meta(TenantBaseModelSerializer.Meta):
        model = CampaignReport
        fields = TenantBaseModelSerializer.Meta.fields + DETAIL_FIELDS
        read_only_fields = fields


class CampaignReportListSerializer(CampaignReportSerializer):
    class Meta(CampaignReportSerializer.Meta):
        fields = TenantBaseModelSerializer.Meta.fields + LIST_FIELDS
        read_only_fields = fields


class CampaignReportDropdownSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = CampaignReport
        fields = ("label", "value")
        read_only_fields = fields

    def get_label(self, obj) -> str:
        return obj.file_name or (
            f"{obj.get_report_format_display()} ({obj.get_status_display()})"
        )


class CampaignReportRequestSerializer(serializers.Serializer):
    """The body of ``POST /campaigns/{id}/reports/``. The campaign is the URL's, and
    resolved through the tenant-scoped queryset, so another tenant's id is a 404."""

    report_format = serializers.ChoiceField(
        choices=CampaignReport.Format.choices,
        default=CampaignReport.Format.XLSX,
    )
