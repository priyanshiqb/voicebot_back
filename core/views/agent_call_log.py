from core.models.agent import Agent
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse, OpenApiExample
from rest_framework.permissions import IsAuthenticated
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
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
import uuid
import os
from django.conf import settings
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status, serializers
from drf_spectacular.utils import inline_serializer
from utils.s3_storage.service import S3StorageService
from rest_framework.exceptions import PermissionDenied

from core.models.agent_call_recording import AgentCallRecording
from core.serializers.agent_call_recording import (
    AgentCallRecordingSerializer,
    RequestRecordingUploadUrlSerializer,
    ConfirmRecordingUploadSerializer
)
from user.models.CustomUser import CustomUser

class AgentCallLogFilter(django_filters.FilterSet):
    class Meta:
        model = AgentCallLog
        fields = ["direction", "outcome", "agent", "campaign_id"]

CALL_LOG_EXAMPLE = {
    "provider_call_id": "call-001",
    "agent": "agent-uuid-string",
    "caller_number": "+91 93880 25735",
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
        description="Get paginated list of agent call logs under the active tenant context.",
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
class AgentCallLogViewSet(TenantBaseViewSet):
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
    search_fields = ["provider_call_id", "caller_number", "caller_name"]
    ordering_fields = ["started_at", "duration_seconds"]
    ordering = ["-started_at"]

    select_related_fields = ["agent"]

    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        Only VB_AGENT can 'create'. Standard Authenticated users can 'list'/'retrieve'.
        """
        if self.action == "create":
            return [IsAuthenticated(), IsVoicebotAgent()]
        return [IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        
        # VB_AGENT users can globally read all call logs across tenants
        if getattr(user, "user_type", None) == CustomUser.UserType.VB_AGENT:
            if self.action in ["list", "retrieve", "dropdown"]:
                # Bypass TenantBaseViewSet tenant scoping and call BaseViewSet directly
                return BaseViewSet.get_queryset(self)
                
        return super().get_queryset()

    def perform_create(self, serializer):
        user = self.request.user
        
        # VB_AGENT bypasses the standard user tenant mapping constraint
        if getattr(user, "user_type", None) == CustomUser.UserType.VB_AGENT:
            # Derive the tenant from the associated Voicebot Agent instead
            agent = serializer.validated_data.get('agent')
            if not agent:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"agent": "Agent must be specified."})
            serializer.save(tenant=agent.tenant)
            return

        super().perform_create(serializer)

    @extend_schema(exclude=True)
    def bulk_delete(self, request, *args, **kwargs):
        from rest_framework.exceptions import MethodNotAllowed
        raise MethodNotAllowed("POST")

    @extend_schema(
        summary="Request a secure S3 upload URL for MP3",
        description="Generates a temporary Presigned PUT URL allowing the AI Agent to upload an MP3 file directly to the storage bucket.",
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
                            "upload_url": serializers.CharField(),
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
        
        user = request.user
        if getattr(user, "user_type", None) == CustomUser.UserType.VB_AGENT:
            agent_id = serializer.validated_data.get("agent")
            if not agent_id:
                return self.error(message="agent field is required for VB_AGENT.", status_code=status.HTTP_400_BAD_REQUEST)
            try:
                agent = Agent.objects.get(id=agent_id, is_deleted=False)
                tenant_id = agent.tenant_id
            except Agent.DoesNotExist:
                return self.error(message="Agent not found.", status_code=status.HTTP_404_NOT_FOUND)
        else:
            try:
                tenant_id = user.tenant_mapping.tenant_id
            except:
                return self.error(message="No active tenant mapping.", status_code=status.HTTP_403_FORBIDDEN)
        
        file_uuid = uuid.uuid4()
        object_key = f"t-{tenant_id}/call_recordings/{file_uuid}_{filename}"
        
        storage_service = S3StorageService()
        try:
            upload_url = storage_service.get_presigned_put_url(object_key, content_type, file_size_in_bytes)
        except Exception as e:
            return self.error(message="Failed to generate upload URL.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
        return self.success(
            message="Upload URL generated successfully.",
            data={
                "upload_url": upload_url,
                "object_key": object_key
            },
            status_code=status.HTTP_200_OK
        )

    @extend_schema(
        summary="Confirm a direct S3 MP3 upload",
        description="Verifies the MP3 arrived in S3 and creates the AgentCallRecording record.",
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
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed or file missing in S3.")
        }
    )
    @action(detail=False, methods=["post"], url_path="confirm-upload")
    def confirm_upload(self, request, *args, **kwargs):
        serializer = ConfirmRecordingUploadSerializer(data=request.data)
        if not serializer.is_valid():
            first_error = next(iter(serializer.errors.values()))[0]
            return self.error(message=str(first_error), status_code=status.HTTP_400_BAD_REQUEST)
            
        object_key = serializer.validated_data["object_key"]
        original_name = serializer.validated_data["original_name"]
        
        extension = serializer.validated_data.get("extension")
        if not extension:
            extension = os.path.splitext(original_name)[1].lower()
            
        storage_service = S3StorageService()
        
        try:
            exists = storage_service.verify_object_exists(object_key)
            if not exists:
                return self.error(message="File not found in storage bucket. Did the upload succeed?", status_code=status.HTTP_400_BAD_REQUEST)
                
            file_stats = storage_service.get_file_disk_usage(object_key)
            # Max 50MB for call recordings
            max_size = 50 * 1024 * 1024
            file_size_in_bytes = file_stats.get("total_size_bytes", 0)
            if file_size_in_bytes > max_size:
                storage_service.delete_blob_direct(object_key)
                return self.error(message="File size exceeds the limit of 50 MB.", status_code=status.HTTP_400_BAD_REQUEST)
                
        except Exception as e:
            return self.error(message="Failed to verify file upload.", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
        user = request.user
        # Resolve tenant
        if getattr(user, "user_type", None) == CustomUser.UserType.VB_AGENT:
            agent_id = serializer.validated_data.get("agent")
            if not agent_id:
                return self.error(message="agent field is required for VB_AGENT.", status_code=status.HTTP_400_BAD_REQUEST)
            try:
                agent = Agent.objects.get(id=agent_id, is_deleted=False)
                tenant = agent.tenant
            except Agent.DoesNotExist:
                return self.error(message="Agent not found.", status_code=status.HTTP_404_NOT_FOUND)
        else:
            try:
                tenant = request.user.tenant_mapping.tenant
            except:
                return self.error(message="No active tenant mapping.", status_code=status.HTTP_403_FORBIDDEN)
                
        recording = AgentCallRecording(
            tenant=tenant,
            original_name=original_name,
            file_size_in_bytes=file_size_in_bytes,
            extension=extension,
            is_processed=False
        )
        
        recording.file.name = object_key
        recording.created_by = request.user
        recording.updated_by = request.user
        
        recording.save()
        
        response_serializer = AgentCallRecordingSerializer(recording)
        
        return self.success(
            message="File confirmed and saved successfully.",
            data=response_serializer.data,
            status_code=status.HTTP_201_CREATED
        )
