import logging
from datetime import datetime, time, timedelta

from django import forms
from django.utils import timezone

from core.models.agent import Agent
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse, OpenApiExample
from rest_framework.permissions import IsAuthenticated
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from tenant.common.views.agent_tenant_scope import (
    VBAgentActionPermission,
    VBAgentTenantScopedMixin,
    assert_agent_in_tenant,
    is_vb_agent,
)
from user.permissions import IsVoicebotAgent
from core.models.agent_call_log import AgentCallLog
from core.serializers.agent_call_log import (
    AgentCallLogSerializer,
    AgentCallLogListSerializer,
    AgentCallLogDropdownSerializer,
)
from onboard.serializers.onboarding import GeneralResponseSerializer
import django_filters
from utils.views.base_model_viewset import BaseViewSet
import os
from django.conf import settings
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status, serializers
from drf_spectacular.utils import inline_serializer
from core.serializers.uploads import upload_ticket_fields
from core.services import uploads as upload_service
from utils.blob_storage.service import AzureBlobStorageService
from utils.blob_storage.keys import (
    build_tenant_object_key,
    is_object_key_owned_by_tenant,
)
from rest_framework.exceptions import PermissionDenied, ValidationError

from core.models.agent_call_recording import AgentCallRecording
from core.serializers.agent_call_recording import (
    AgentCallRecordingSerializer,
    RequestRecordingUploadUrlSerializer,
    ConfirmRecordingUploadSerializer
)

logger = logging.getLogger(__name__)

#: Size ceiling for a call recording. Hoisted to a constant because it is now needed in
#: two places that must not disagree: signed into the upload ticket, and re-checked
#: against the stored object at confirm. A limit enforced at only one of those is either
#: bypassable or arbitrarily strict.
MAX_RECORDING_BYTES = 50 * 1024 * 1024


class CharInFilter(django_filters.BaseInFilter, django_filters.CharFilter):
    pass


def filter_call_date_range(queryset, start_date=None, end_date=None):
    """Narrow to calls whose ``created_at`` falls within ``[start_date, end_date]``.

    Both bounds are inclusive calendar days, either may be None, and both are read in
    the server timezone (UTC) — the same zone the dashboard resolves its `from`/`to`
    in, so "calls in September" cannot mean two different things depending on which
    endpoint asked.

    Filtering is on ``created_at``, not ``started_at``, for the same reason the
    dashboard does it: ``started_at`` is nullable, and a range filter on a nullable
    column silently drops every row that lacks one — a call the runner posted without
    a start time would vanish from the range rather than being counted late, and
    nothing about the response would look wrong. ``created_at`` is always present, so
    a range adds up to the calls that exist, and the list agrees with the dashboard
    totals a caller clicked through from.

    The trade is that a log posted well after the call ended is filed on the day it
    arrived rather than the day it happened. That is visible and explainable; silently
    missing calls are neither.
    """
    if start_date is None and end_date is None:
        return queryset

    if start_date is not None:
        queryset = queryset.filter(
            created_at__gte=timezone.make_aware(datetime.combine(start_date, time.min))
        )
    if end_date is not None:
        # Half-open at the top — "before the next midnight" rather than
        # "at or before 23:59:59.999999". Postgres stores microseconds, so the
        # inclusive form drops a call filed in the last microsecond of its own day.
        queryset = queryset.filter(
            created_at__lt=timezone.make_aware(datetime.combine(end_date, time.min))
            + timedelta(days=1)
        )
    return queryset


class AgentCallLogFilterForm(forms.Form):
    """Cross-field validation for the date range.

    An inverted range is rejected rather than answered with an empty list: zero rows
    is a legitimate answer to a well-formed question, so returning it for a malformed
    one leaves the caller debugging their data instead of their query string.
    """

    def clean(self):
        cleaned = super().clean()
        start_date, end_date = cleaned.get("start_date"), cleaned.get("end_date")
        if start_date and end_date and end_date < start_date:
            raise forms.ValidationError(
                {"end_date": "end_date must be on or after start_date."}
            )
        return cleaned


class AgentCallLogFilter(django_filters.FilterSet):
    outcome = CharInFilter(field_name="outcome", lookup_expr="in")
    start_date = django_filters.DateFilter(
        method="filter_from_date",
        label="Calls logged on or after this UTC date, inclusive (YYYY-MM-DD).",
    )
    end_date = django_filters.DateFilter(
        method="filter_to_date",
        label="Calls logged up to this UTC date, INCLUSIVE of the whole day (YYYY-MM-DD).",
    )

    class Meta:
        model = AgentCallLog
        fields = ["direction", "agent", "campaign_id"]
        form = AgentCallLogFilterForm

    def filter_from_date(self, queryset, name, value):
        return filter_call_date_range(queryset, start_date=value)

    def filter_to_date(self, queryset, name, value):
        return filter_call_date_range(queryset, end_date=value)

CALL_LOG_EXAMPLE = {
    "provider_call_id": "call-001",
    "agent": "agent-uuid-string",
    # Inbound: the customer dialled the bot, so the bot's DID is the callee.
    "caller_number": "+91 93880 25735",
    "callee_number": "+91 80 4718 2200",
    "caller_name": "Nisha Patel",
    "direction": "inbound",
    "duration_seconds": 326,
    "outcome": "order_placed",
    "mood": "neutral",
    "mood_confidence": 0.85,
    "pii_redacted": False,
    "ai_summary": "Prospect interested in 2BHK property.",
    "preferred_language": "Spanish",
    "probing_questions_count": 3,
    "probing_questions": [
        {
            "question": "What is your monthly estimated call volume?",
            "answerSummary": "Over 50,000 calls per month.",
            "isPositive": True
        }
    ],
    "follow_up_detail": "Follow-up with alternative plans after contract review.",
    "human_transfer_detail": None,
    "transcript": [
        {
            "id": "tl-0",
            "speaker": "bot",
            "text": "Hello! Thank you for calling.",
            "timestamp": 0,
            "isRedacted": False
        }
    ],
    "recording_file": "123e4567-e89b-12d3-a456-426614174000",
    "started_at": "2026-06-23T17:33:31.671Z",
    "ended_at": "2026-06-23T17:38:57.671Z",
    "tags": ["urgent"]
}

@extend_schema(tags=["Agent Call Log"])
@extend_schema_view(
    list=extend_schema(
        summary="List Call Logs",
        description=(
            "Get paginated list of agent call logs under the active tenant context.\n\n"
            "Filter by `direction`, `agent`, `campaign_id`, `outcome` (comma-separated "
            "for several), and `start_date` / `end_date` — inclusive UTC calendar days, "
            "matched on `created_at` (when the log was written), which is the same "
            "window the dashboard counts."
        ),
        responses={200: AgentCallLogListSerializer(many=True)}
    ),
    retrieve=extend_schema(
        summary="Get Call Log",
        description="Get specific call log details by ID.",
        responses={200: AgentCallLogSerializer}
    ),
    create=extend_schema(
        summary="Create Call Log",
        description="Create a new call log. STRICTLY RESTRICTED to Voicebot Agent tokens.",
        request=AgentCallLogSerializer,
        examples=[
            OpenApiExample(
                name="Standard Call Telemetry",
                description="A complete call log telemetry payload",
                value=CALL_LOG_EXAMPLE,
                request_only=True
            )
        ],
        responses={
            201: AgentCallLogSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid input values or validation error."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied. Only VB_AGENT can access this.")
        }
    ),
    dropdown=extend_schema(
        summary="Get Dropdown Options",
        description="Get lightweight label/value selection pairs representing call logs.",
        responses={200: AgentCallLogDropdownSerializer(many=True)}
    )
)
class AgentCallLogViewSet(VBAgentTenantScopedMixin, TenantBaseViewSet):
    """
    Agent Call Log API.
    Provides read-only access to standard users and write access to Voicebot Agents.
    """
    http_method_names = ["get", "post", "options"]
    
    queryset = AgentCallLog.objects.all()
    serializer_class = AgentCallLogSerializer
    list_serializer_class = AgentCallLogListSerializer
    dropdown_serializer_class = AgentCallLogDropdownSerializer

    filterset_class = AgentCallLogFilter
    # callee_number included so searching a customer's number finds outbound calls
    # too — on those the person is the callee, not the caller.
    search_fields = [
        "provider_call_id",
        "caller_number",
        "callee_number",
        "caller_name",
    ]
    ordering_fields = ["started_at", "duration_seconds"]
    ordering = ["-started_at"]

    select_related_fields = ["agent"]

    # Full access: this viewset exists for machine callers. `bulk_delete` is absent
    # because it is MethodNotAllowed for everyone, and there is no update/destroy —
    # http_method_names above already limits this to GET and POST.
    vb_agent_allowed_actions = frozenset({
        "list",
        "retrieve",
        "dropdown",
        "create",
        "request_upload_url",
        "confirm_upload",
    })

    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        Only VB_AGENT can 'create'. Standard Authenticated users can 'list'/'retrieve'.

        VBAgentActionPermission is included explicitly: returning a list here replaces
        `permission_classes` wholesale, so the class BaseViewSet sets would otherwise be
        dropped and every action would silently reopen to machine callers.
        """
        if self.action == "create":
            return [IsAuthenticated(), IsVoicebotAgent(), VBAgentActionPermission()]
        return [IsAuthenticated(), VBAgentActionPermission()]

    def get_queryset(self):
        user = self.request.user
        
        # VB_AGENT reads call logs across tenants, narrowed to the tenant named in
        # `X-Tenant-ID`, which is mandatory.
        if is_vb_agent(user):
            if self.action in ["list", "retrieve", "dropdown"]:
                # Bypass TenantBaseViewSet tenant scoping and call BaseViewSet directly
                return self.scope_to_agent_tenant(BaseViewSet.get_queryset(self))

        return super().get_queryset()

    def perform_create(self, serializer):
        user = self.request.user

        # VB_AGENT bypasses the standard user tenant mapping constraint
        if is_vb_agent(user):
            # The header is the authority on where this row lands — not the agent. The
            # two are required to agree (below), but writing the header tenant is what
            # keeps this honest if that check is ever relaxed.
            tenant = self.get_agent_tenant()

            agent = serializer.validated_data.get('agent')
            if not agent:
                raise ValidationError({"agent": "Agent must be specified."})
            assert_agent_in_tenant(agent, tenant)

            serializer.save(tenant=tenant)
            self._charge_credits(serializer.instance)
            return

        super().perform_create(serializer)
        self._charge_credits(serializer.instance)

    def _charge_credits(self, call_log):
        """Deduct the cost of this call from the tenant's wallet.

        **Never allowed to fail the request.** The call already happened and the log is
        already saved; raising here would make the runner retry a POST whose row is
        committed, and every retry would be a fresh attempt to charge for the same
        call. A billing problem is ours to reconcile, not the runner's to retry into —
        so anything unexpected is logged and swallowed, and `report_unrated_calls`
        sweeps up what was missed.

        Idempotent by `provider_call_id`, which is already unique on the model. A
        re-posted call writes nothing the second time.
        """
        from wallet.services import policy
        from wallet.services import wallet as wallet_service

        try:
            billable = policy.operation_for_call(call_log)
            if billable is None:
                # No duration yet — nothing to rate. This viewset is POST-only, so a
                # call log is written once; a row arriving with no duration means the
                # runner did not have one, and it will be reported as unrated rather
                # than guessed at.
                logger.info(
                    "call %s has no duration; not charged", call_log.provider_call_id
                )
                return

            operation, quantity = billable
            wallet_service.consume(
                call_log.tenant_id,
                operation=operation,
                quantity=quantity,
                reference_id=call_log.provider_call_id,
                metadata={
                    "agent_id": str(call_log.agent_id),
                    "direction": call_log.direction,
                },
            )
        except Exception:
            logger.exception(
                "failed to charge credits for call %s (tenant %s)",
                call_log.provider_call_id,
                call_log.tenant_id,
            )

    @extend_schema(exclude=True)
    def bulk_delete(self, request, *args, **kwargs):
        from rest_framework.exceptions import MethodNotAllowed
        raise MethodNotAllowed("POST")

    def _resolve_recording_tenant_id(self, request, serializer):
        """
        Resolve the tenant that owns a recording upload.

        AI-team callers (VB_AGENT) have no tenant mapping, so their tenant is the one
        named in `X-Tenant-ID`. Both upload steps must resolve it the same way — a key
        authorized against the wrong tenant is exactly the bug this guards.

        Returns (tenant_id, error_response). Exactly one is not None.
        """
        user = request.user

        if is_vb_agent(user):
            # The header decides the tenant, and therefore the object-key prefix. The
            # `agent` is still required and still has to live in that tenant, but it
            # only corroborates the scope — it never sets it.
            tenant = self.get_agent_tenant()

            agent_id = serializer.validated_data.get("agent")
            if not agent_id:
                return None, self.error(message="agent field is required for VB_AGENT.", status_code=status.HTTP_400_BAD_REQUEST)
            try:
                agent = Agent.objects.get(id=agent_id, is_deleted=False)
            except Agent.DoesNotExist:
                return None, self.error(message="Agent not found.", status_code=status.HTTP_404_NOT_FOUND)
            if agent.tenant_id != tenant.id:
                return None, self.error(
                    message="The specified agent does not belong to the requested tenant.",
                    status_code=status.HTTP_403_FORBIDDEN,
                )
            return tenant.id, None

        try:
            return user.tenant_mapping.tenant_id, None
        except Exception:
            return None, self.error(message="No active tenant mapping.", status_code=status.HTTP_403_FORBIDDEN)

    @extend_schema(
        summary="Request a secure upload URL for MP3",
        description="Generates a short-lived signed (SAS) PUT URL allowing the AI Agent to upload an MP3 file directly to blob storage. The response's upload_headers must be sent with that PUT.",
        request=RequestRecordingUploadUrlSerializer,
        responses={
            200: inline_serializer(
                name="RequestRecordingUploadUrlResponse",
                fields={
                    "status": serializers.BooleanField(),
                    "message": serializers.CharField(),
                    "data": inline_serializer(
                        name="RequestRecordingUploadUrlData",
                        fields={
                            **upload_ticket_fields(),
                            "object_key": serializers.CharField()
                        }
                    )
                }
            ),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed.")
        }
    )
    @action(detail=False, methods=["post"], url_path="request-upload-url")
    def request_upload_url(self, request, *args, **kwargs):
        serializer = RequestRecordingUploadUrlSerializer(data=request.data)
        if not serializer.is_valid():
            first_error = next(iter(serializer.errors.values()))[0]
            return self.error(message=str(first_error), status_code=status.HTTP_400_BAD_REQUEST)
        
        filename = serializer.validated_data["filename"]
        content_type = serializer.validated_data["content_type"]
        file_size_in_bytes = serializer.validated_data["file_size_in_bytes"]

        tenant_id, error_response = self._resolve_recording_tenant_id(request, serializer)
        if error_response is not None:
            return error_response

        # Create the record up front so the object key can live server-side and be
        # looked up by id at confirm time. The client never needs to hand it back.
        recording = AgentCallRecording(
            tenant_id=tenant_id,
            original_name=filename[:255],
            extension=os.path.splitext(filename)[1].lower(),
            file_size_in_bytes=file_size_in_bytes,
            upload_status=AgentCallRecording.UploadStatus.PENDING,
            is_processed=False,
        )
        # The filename is sanitized to a basename so a crafted value cannot steer the
        # key outside this tenant's prefix.
        object_key = build_tenant_object_key(
            tenant_id, "call_recordings", filename, unique_id=recording.id
        )
        recording.file.name = object_key
        recording.save()

        # Same 50 MB ceiling confirm-upload applies, handed to the ticket so that under
        # POST the object store enforces it during the upload instead of after it.
        try:
            ticket = upload_service.build_upload_ticket(
                object_key,
                content_type,
                max_bytes=MAX_RECORDING_BYTES,
                storage=AzureBlobStorageService(),
            )
        except Exception:
            logger.exception("Could not presign call recording upload for tenant %s", tenant_id)
            recording.upload_status = AgentCallRecording.UploadStatus.FAILED
            recording.save(update_fields=["upload_status"])
            return self.error(message="Failed to generate upload URL.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return self.success(
            message="Upload URL generated successfully.",
            data={
                "file_id": str(recording.id),
                **ticket,
                # Deprecated: kept so existing clients that echo it back keep working.
                "object_key": object_key
            },
            status_code=status.HTTP_200_OK
        )

    @extend_schema(
        summary="Confirm a direct MP3 upload",
        description="Verifies the MP3 arrived in blob storage and creates the AgentCallRecording record.",
        request=ConfirmRecordingUploadSerializer,
        responses={
            201: inline_serializer(
                name="ConfirmRecordingUploadResponse",
                fields={
                    "status": serializers.BooleanField(),
                    "message": serializers.CharField(),
                    "data": AgentCallRecordingSerializer()
                }
            ),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed or file missing in blob storage.")
        }
    )
    @action(detail=False, methods=["post"], url_path="confirm-upload")
    def confirm_upload(self, request, *args, **kwargs):
        serializer = ConfirmRecordingUploadSerializer(data=request.data)
        if not serializer.is_valid():
            first_error = next(iter(serializer.errors.values()))[0]
            return self.error(message=str(first_error), status_code=status.HTTP_400_BAD_REQUEST)
            
        file_id = serializer.validated_data.get("file_id")
        object_key = serializer.validated_data.get("object_key")
        original_name = serializer.validated_data.get("original_name")

        # Security: resolve the tenant BEFORE touching blob storage. The oversize branch below
        # deletes the object it was handed, so an unauthorized key must never reach it.
        tenant_id, error_response = self._resolve_recording_tenant_id(request, serializer)
        if error_response is not None:
            return error_response

        recording = None
        if file_id:
            # Preferred path: the key is read from a tenant-scoped row, so it is not
            # possible to name a recording belonging to another tenant.
            recording = AgentCallRecording.objects.filter(
                id=file_id, tenant_id=tenant_id
            ).first()
            if recording is None:
                return self.error(message="Recording not found.", status_code=status.HTTP_404_NOT_FOUND)
            object_key = recording.file.name
            original_name = original_name or recording.original_name
        else:
            # Deprecated path: still fully authorized, but log it so the transition
            # window can be closed once clients stop using it.
            logger.info(
                "confirm-upload called with deprecated object_key by user %s; "
                "clients should send file_id instead.",
                request.user.pk,
            )
            # Security: the key must live inside this tenant's namespace. Report 404
            # rather than 403 so the response cannot probe for another tenant's objects.
            if not is_object_key_owned_by_tenant(object_key, tenant_id, folder="call_recordings"):
                return self.error(message="File not found in storage. Did the upload succeed?", status_code=status.HTTP_404_NOT_FOUND)

        extension = serializer.validated_data.get("extension")
        if not extension:
            extension = os.path.splitext(original_name or "")[1].lower()

        storage_service = AzureBlobStorageService()

        try:
            exists = storage_service.verify_object_exists(object_key)
            if not exists:
                return self.error(message="File not found in storage. Did the upload succeed?", status_code=status.HTTP_400_BAD_REQUEST)

            file_stats = storage_service.get_file_disk_usage(object_key)
            max_size = MAX_RECORDING_BYTES
            file_size_in_bytes = file_stats.get("total_size_bytes", 0)
            if file_size_in_bytes > max_size:
                storage_service.delete_blob_direct(object_key)
                if recording is not None:
                    recording.upload_status = AgentCallRecording.UploadStatus.FAILED
                    recording.save(update_fields=["upload_status"])
                return self.error(message="File size exceeds the limit of 50 MB.", status_code=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            return self.error(message="Failed to verify file upload.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # What the file claims to be versus what it is. `content_type` was restricted to
        # MP3 at step 1, but that is a client string — nothing has looked at the bytes.
        # Fails closed, as the size check above does.
        try:
            mismatch, detected = upload_service.content_mismatch(
                storage_service, object_key, extension
            )
        except Exception:
            logger.exception("Could not read the head bytes of recording %s", object_key)
            return self.error(
                message="Failed to verify file upload.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if mismatch:
            # Authorization already ran above, so this key is ours to delete.
            try:
                storage_service.delete_blob_direct(object_key)
            except Exception:
                logger.warning("Rejected recording %s could not be purged.", object_key)
            if recording is not None:
                recording.upload_status = AgentCallRecording.UploadStatus.FAILED
                recording.save(update_fields=["upload_status"])
            return self.error(
                message=(
                    f"That file is not {extension or 'the declared type'} — its "
                    f"contents look like {detected}. Upload the real file."
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if recording is None:
            # Legacy object_key path: no row was reserved up front, so create one now.
            recording = AgentCallRecording(tenant_id=tenant_id, is_processed=False)
            recording.file.name = object_key

        recording.original_name = original_name or recording.original_name
        recording.extension = extension or recording.extension
        recording.file_size_in_bytes = file_size_in_bytes
        recording.upload_status = AgentCallRecording.UploadStatus.CONFIRMED
        recording.save()

        response_serializer = AgentCallRecordingSerializer(recording)
        
        return self.success(
            message="File confirmed and saved successfully.",
            data=response_serializer.data,
            status_code=status.HTTP_201_CREATED
        )
