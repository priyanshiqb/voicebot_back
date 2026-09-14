"""Campaign reports: poll, download.

A report is requested on its campaign — ``POST /campaigns/{id}/reports/``, in
``core/views/campaign.py`` — which writes a PENDING row and returns 202 at once. The file
is built by a Celery task. This viewset is the read side: the client polls a report here
until it is ``COMPLETED``, then asks for a download link.

Read-only on purpose. A report is moved only by the task that builds it, and removed only
by the expiry sweep, which deletes its file first.
"""

from drf_spectacular.utils import (
    OpenApiResponse,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.decorators import action

from core.models.campaign_report import CampaignReport
from core.serializers.campaign_report import (
    CampaignReportDropdownSerializer,
    CampaignReportListSerializer,
    CampaignReportSerializer,
)
from core.services import campaign_reports as report_service
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet

NOT_READY_MESSAGES = {
    CampaignReport.Status.PENDING: "This report is queued and not ready yet.",
    CampaignReport.Status.RUNNING: "This report is still being generated.",
    CampaignReport.Status.FAILED: "This report failed and has no file. Request a new one.",
    CampaignReport.Status.EXPIRED: "This report has expired. Request a new one.",
}


@extend_schema(tags=["Campaign Reports"])
@extend_schema_view(
    list=extend_schema(
        summary="List Campaign Reports",
        description=(
            "This tenant's reports, newest first. Filter with `?campaign=`, `?status=` "
            "and `?report_format=`."
        ),
    ),
    retrieve=extend_schema(
        summary="Get Campaign Report",
        description=(
            "Poll this after requesting a report: `status`, `progress_percent` and "
            "`is_downloadable`. One row, no joins — cheap enough to call every few seconds."
        ),
    ),
    dropdown=extend_schema(summary="Campaign Report Options"),
)
class CampaignReportViewSet(TenantBaseViewSet):
    queryset = CampaignReport.objects.all()
    serializer_class = CampaignReportSerializer
    list_serializer_class = CampaignReportListSerializer
    dropdown_serializer_class = CampaignReportDropdownSerializer

    http_method_names = ["get", "head", "options"]
    # Inherited as a POST route. GET-only already answers it with a 405, but shadowing
    # it with a non-action unregisters the route, so the schema does not advertise it.
    bulk_delete = None

    filterset_fields = ["campaign", "status", "report_format"]
    ordering_fields = ["created_at", "completed_at"]
    ordering = ["-created_at"]
    select_related_fields = ["tenant", "campaign", "created_by", "updated_by", "deleted_by"]

    @extend_schema(
        summary="Download a Campaign Report",
        description=(
            "Returns a fresh, short-lived link to the file as JSON rather than redirecting. "
            "This endpoint needs a bearer token, which a plain link cannot carry, and a "
            "`fetch` that followed a redirect would pull the file into JavaScript instead "
            "of the downloads folder. So: call this, then navigate to `download_url`. The "
            "link is minted per call and never stored."
        ),
        responses={
            200: OpenApiResponse(
                OpenApiTypes.OBJECT,
                description=(
                    "`download_url`, `file_name`, `file_size_bytes`, `expires_in_seconds`."
                ),
            ),
            404: OpenApiResponse(description="No such report in your organisation."),
            409: OpenApiResponse(description="The report is not ready, failed, or expired."),
        },
    )
    @action(detail=True, methods=["get"])
    def download(self, request, *args, **kwargs):
        report = self.get_object()
        if not report.is_downloadable:
            return self.error(
                message=NOT_READY_MESSAGES.get(report.status, "This report has no file."),
                status_code=status.HTTP_409_CONFLICT,
            )
        return self.success(
            data=report_service.download_payload(report),
            message="Download link issued",
        )
