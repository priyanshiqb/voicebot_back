import os

from django.conf import settings
from rest_framework import serializers

from utils.files.sizes import format_size_limit

from core.models.contact_import import (
    ContactImportFile,
    ContactImportRow,
    ContactImportSession,
)
from core.services.contact_import import CANONICAL_COLUMNS
from utils.phone import is_supported_region, resolve_region

ALLOWED_EXTENSIONS = [".csv"]
ALLOWED_CONTENT_TYPES = [
    "text/csv",
    "application/csv",
    "text/plain",
    "application/vnd.ms-excel",  # what Windows reports for .csv
]


class RequestContactUploadUrlSerializer(serializers.Serializer):
    filename = serializers.CharField(max_length=255)
    content_type = serializers.CharField(max_length=100)
    file_size_in_bytes = serializers.IntegerField(min_value=1)

    def validate_filename(self, value):
        extension = os.path.splitext(value)[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise serializers.ValidationError(
                f"Unsupported format. Only {', '.join(ALLOWED_EXTENSIONS)} is allowed."
            )
        return value

    def validate_content_type(self, value):
        if value.lower() not in ALLOWED_CONTENT_TYPES:
            raise serializers.ValidationError(
                "Content type must be text/csv."
            )
        return value

    def validate_file_size_in_bytes(self, value):
        max_size = getattr(settings, "CONTACT_IMPORT_MAX_FILE_BYTES", 10 * 1024 * 1024)
        if value > max_size:
            raise serializers.ValidationError(
                f"File size exceeds the limit of {format_size_limit(max_size)}."
            )
        return value


class ConfirmContactUploadSerializer(serializers.Serializer):
    """
    Takes only the file_id. The object key stays server-side, so a caller cannot name
    — and then import — a file belonging to another tenant.
    """

    file_id = serializers.UUIDField()


class ContactImportFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContactImportFile
        fields = [
            "id",
            "original_name",
            "extension",
            "file_size_in_bytes",
            "upload_status",
            "created_at",
        ]
        read_only_fields = fields


class StartContactImportSerializer(serializers.Serializer):
    file_id = serializers.UUIDField()
    default_region = serializers.CharField(
        max_length=2, required=False, allow_blank=True
    )
    conflict_strategy = serializers.ChoiceField(
        choices=ContactImportSession.ConflictStrategy.choices,
        required=False,
        default=ContactImportSession.ConflictStrategy.SKIP,
    )
    column_map = serializers.DictField(
        child=serializers.ChoiceField(choices=CANONICAL_COLUMNS),
        required=False,
    )

    def validate(self, attrs):
        """
        A region is mandatory. Numbers that are not already in international form
        cannot be parsed without one, and guessing silently rewrites them to the
        wrong country — so a missing region is rejected before any work starts.
        """
        region = resolve_region(attrs.get("default_region"))
        if not region:
            raise serializers.ValidationError(
                {
                    "default_region": (
                        "A region is required. Pass default_region (ISO-3166 alpha-2, "
                        "e.g. 'US'), or set CONTACT_DEFAULT_PHONE_REGION on the server."
                    )
                }
            )
        if not is_supported_region(region):
            raise serializers.ValidationError(
                {"default_region": f"'{region}' is not a recognised region code."}
            )

        attrs["default_region"] = region
        return attrs


from drf_spectacular.utils import extend_schema_field

class ContactImportSessionSerializer(serializers.ModelSerializer):
    import_file = ContactImportFileSerializer(read_only=True)
    created_by_email = serializers.CharField(
        source="created_by.email", read_only=True, default=None
    )
    committed_by_email = serializers.CharField(
        source="committed_by.email", read_only=True, default=None
    )
    counts = serializers.SerializerMethodField()

    class Meta:
        model = ContactImportSession
        fields = [
            "id",
            "import_file",
            "status",
            "default_region",
            "conflict_strategy",
            "column_map",
            "expires_at",
            "counts",
            "error_message",
            "notices",
            "created_by",
            "created_by_email",
            "committed_by",
            "committed_by_email",
            "committed_at",
            "discarded_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.DictField())
    def get_counts(self, obj):
        return {
            "total": obj.total_rows,
            "valid": obj.valid_rows,
            "conflicts": obj.conflict_rows,
            "invalid": obj.invalid_rows,
            "created": obj.created_count,
            "updated": obj.updated_count,
            "skipped": obj.skipped_count,
            "failed": obj.failed_count,
        }


class ContactImportRowSerializer(serializers.ModelSerializer):
    conflict_contact_id = serializers.UUIDField(
        source="conflict_contact.id", read_only=True, default=None
    )

    class Meta:
        model = ContactImportRow
        fields = [
            "id",
            "session",
            "row_number",
            "raw_data",
            "parsed_data",
            "phone_e164",
            "status",
            "resolution",
            "conflict_contact_id",
            "error_message",
            "updated_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ContactImportRowUpdateSerializer(serializers.Serializer):
    """
    The only writable surface on a staged row.

    `status` is deliberately absent: it is re-derived from validation after the edit.
    Letting a client set it would allow marking a row resolved without fixing the
    duplicate, which then blows up at commit.
    """

    parsed_data = serializers.DictField(
        child=serializers.CharField(allow_blank=True, required=False),
        required=False,
    )
    resolution = serializers.ChoiceField(
        choices=ContactImportRow.Resolution.choices, required=False, allow_blank=True
    )

    def validate_parsed_data(self, value):
        unknown = set(value) - set(CANONICAL_COLUMNS)
        if unknown:
            raise serializers.ValidationError(
                f"Unknown field(s): {', '.join(sorted(unknown))}."
            )
        return value

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Provide parsed_data and/or resolution."
            )
        return attrs


class BulkResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=ContactImportRow.Status.choices)
    resolution = serializers.ChoiceField(choices=ContactImportRow.Resolution.choices)


class CommitContactImportSerializer(serializers.Serializer):
    ignore_unresolved = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Skip rows still flagged as conflicts instead of refusing to commit.",
    )
