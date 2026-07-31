from django.http import HttpResponse
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status, serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import MultiPartParser, FormParser
from drf_spectacular.utils import extend_schema, inline_serializer
from onboard.serializers.onboarding import GeneralResponseSerializer
from import_export.results import RowResult
import tablib

class ImportExportMixin:
    """
    Provides import and export actions for a ViewSet using django-import-export.
    Requires `resource_class` to be defined on the ViewSet.

    Both actions are tenant-scoped work and require a caller with a tenant. Machine
    (`VB_AGENT`) callers have no tenant mapping and are refused — see
    `_resolve_import_tenant`.
    """
    resource_class = None

    def _resolve_import_tenant(self, request):
        """
        The tenant a bulk import/export runs against, as (tenant, allow_cross_tenant).

        Superusers keep the global access they already have throughout
        `TenantBaseViewSet`. Every other caller must have an active tenant mapping.

        This is enforced here rather than left to the viewset's `get_queryset`, because
        `import_data` never calls it: the tenant guard that protects every other write
        on these viewsets simply does not run on the import path. Machine callers are
        the concrete case — bulk CSV writes are not theirs to perform, and without this
        a VB_AGENT token could update any tenant's rows by naming their ids.
        """
        user = request.user

        if user.is_superuser:
            return None, True

        try:
            return user.tenant_mapping.tenant, False
        except Exception:
            raise PermissionDenied(
                "Your user account is not associated with any active tenant."
            )

    @extend_schema(
        summary="Export Data",
        description=(
            "Export data in CSV or Excel format. Use ?file_format=csv or ?file_format=xlsx.\n\n"
            "Note: `?format=` cannot be used here — DRF reserves it for content "
            "negotiation and rejects unknown values with a 404 before this view runs."
        ),
        responses={200: bytes}
    )
    @action(detail=False, methods=['get'], url_path='export')
    def export_data(self, request, *args, **kwargs):
        if not self.resource_class:
            return Response({"status": False, "message": "Export not supported for this resource."}, status=status.HTTP_501_NOT_IMPLEMENTED)

        # Checked explicitly rather than relying on get_queryset to raise. It does today,
        # but only because "export_data" happens not to appear in the VB_AGENT bypass
        # lists in agent.py / agent_call_log.py — adding it there must not silently turn
        # this into a cross-tenant data dump.
        self._resolve_import_tenant(request)

        queryset = self.filter_queryset(self.get_queryset())
        resource = self.resource_class()
        dataset = resource.export(queryset)

        # `format` is DRF's URL_FORMAT_OVERRIDE: passing ?format=xlsx never reaches this
        # method, it 404s during content negotiation. `file_format` is the usable param;
        # `format` is still read so any caller that somehow sets it keeps working.
        file_format = (
            request.query_params.get('file_format')
            or request.query_params.get('format')
            or 'csv'
        ).lower()
        basename = getattr(self, 'basename', 'export')
        
        if file_format == 'xlsx':
            response = HttpResponse(dataset.xlsx, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = f'attachment; filename="{basename}.xlsx"'
        else:
            response = HttpResponse(dataset.csv, content_type='text/csv')
            response['Content-Disposition'] = f'attachment; filename="{basename}.csv"'
        return response

    @extend_schema(
        summary="Import Data",
        description="Import data from CSV or Excel file.",
        request=inline_serializer(
            name="ImportDataSerializer",
            fields={
                "file": serializers.FileField(required=True)
            }
        ),
        responses={200: GeneralResponseSerializer, 400: GeneralResponseSerializer}
    )
    @action(detail=False, methods=['post'], url_path='import', parser_classes=[MultiPartParser, FormParser])
    def import_data(self, request, *args, **kwargs):
        if not self.resource_class:
            return Response({"status": False, "message": "Import not supported for this resource."}, status=status.HTTP_501_NOT_IMPLEMENTED)

        # Before the file is read: a caller with no tenant has nothing to import into.
        self._resolve_import_tenant(request)

        file = request.FILES.get('file')
        if not file:
            return Response({"status": False, "message": "No file provided"}, status=status.HTTP_400_BAD_REQUEST)
        
        ext = file.name.split('.')[-1].lower()
        dataset = tablib.Dataset()
        
        try:
            if ext == 'csv':
                dataset.load(file.read().decode('utf-8'), format='csv')
            elif ext in ['xls', 'xlsx']:
                dataset.load(file.read(), format=ext)
            else:
                return Response({"status": False, "message": "Unsupported file format. Use csv or xlsx."}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"status": False, "message": f"Failed to parse file: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
            
        # Validate first, then write. A fresh resource per run keeps per-run state from
        # leaking between the dry run and the real import.
        dry_result = self._run_import(request, dataset, dry_run=True)
        if self._import_failed(dry_result):
            return Response(
                {
                    "status": False,
                    "message": "Import failed due to validation errors.",
                    "errors": self._collect_import_errors(dry_result),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = self._run_import(request, dataset, dry_run=False)
        # Defensive: the dry run catches the common cases, but its result is not a
        # guarantee for the real write (a concurrent insert can land in between). The
        # previous code discarded this result entirely, so any such failure would have
        # been reported as success.
        if self._import_failed(result):
            return Response(
                {
                    "status": False,
                    "message": "Import failed and was rolled back.",
                    "errors": self._collect_import_errors(result),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        created = result.totals.get(RowResult.IMPORT_TYPE_NEW, 0)
        updated = result.totals.get(RowResult.IMPORT_TYPE_UPDATE, 0)
        skipped = result.totals.get(RowResult.IMPORT_TYPE_SKIP, 0)

        return Response({
            "status": True,
            # Counts come from the result, not len(dataset) — the file's row count is
            # not the number of records actually written.
            "message": f"Imported {created} new and {updated} updated record(s).",
            "data": {"created": created, "updated": updated, "skipped": skipped},
        })

    def _run_import(self, request, dataset, dry_run):
        resource = self.resource_class()

        # Inject tenant and user context for the resource to use during save.
        # Assigned unconditionally, including the None/True superuser case: the previous
        # `if hasattr(...)` left `tenant` unset for anyone without a mapping, and the
        # resource read that absence as "unscoped".
        tenant, allow_cross_tenant = self._resolve_import_tenant(request)
        resource.tenant = tenant
        resource.allow_cross_tenant = allow_cross_tenant
        resource.user = request.user

        return resource.import_data(
            dataset,
            dry_run=dry_run,
            # All-or-nothing: a half-applied bulk import is worse than a rejected one.
            use_transactions=True,
            rollback_on_validation_errors=True,
        )

    @staticmethod
    def _import_failed(result):
        # has_errors() covers only critical (non-validation) errors — the library is
        # explicit about that. Validation errors are tracked separately.
        #
        # invalid_rows is only populated when a resource sets
        # Meta.clean_model_instances = True (it defaults to False), so this arm is
        # currently inert. It is here so enabling that flag on a resource does not
        # silently start dropping rows while the response reports success.
        return result.has_errors() or result.has_validation_errors()

    @staticmethod
    def _collect_import_errors(result):
        errors = []

        for row_number, row_errors in result.row_errors():
            for err in row_errors:
                errors.append({"row": row_number, "error": str(err.error)})

        for invalid_row in result.invalid_rows:
            for field, messages in invalid_row.error_dict.items():
                errors.append(
                    {"row": invalid_row.number, "field": field, "error": "; ".join(messages)}
                )

        return errors
