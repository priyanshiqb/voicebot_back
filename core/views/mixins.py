from django.http import HttpResponse
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status, serializers
from rest_framework.parsers import MultiPartParser, FormParser
from drf_spectacular.utils import extend_schema, inline_serializer
from onboard.serializers.onboarding import GeneralResponseSerializer
import tablib

class ImportExportMixin:
    """
    Provides import and export actions for a ViewSet using django-import-export.
    Requires `resource_class` to be defined on the ViewSet.
    """
    resource_class = None

    @extend_schema(
        summary="Export Data",
        description="Export data in CSV or Excel format. Use ?format=csv or ?format=xlsx",
        responses={200: bytes}
    )
    @action(detail=False, methods=['get'], url_path='export')
    def export_data(self, request, *args, **kwargs):
        if not self.resource_class:
            return Response({"status": False, "message": "Export not supported for this resource."}, status=status.HTTP_501_NOT_IMPLEMENTED)
            
        queryset = self.filter_queryset(self.get_queryset())
        resource = self.resource_class()
        dataset = resource.export(queryset)
        
        file_format = request.query_params.get('format', 'csv').lower()
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
            
        resource = self.resource_class()
        
        # Inject tenant and user context for the resource to use during save
        if hasattr(request.user, 'tenant_mapping'):
            resource.tenant = request.user.tenant_mapping.tenant
        resource.user = request.user
        
        result = resource.import_data(dataset, dry_run=True)
        if not result.has_errors():
            resource.import_data(dataset, dry_run=False)
            return Response({"status": True, "message": f"Successfully imported {len(dataset)} records."})
        else:
            # Flatten errors for the response
            errors = []
            for row_error in result.row_errors():
                for err in row_error[1]:
                    errors.append({"row": row_error[0], "error": str(err.error)})
            return Response({"status": False, "message": "Import failed due to validation errors.", "errors": errors}, status=status.HTTP_400_BAD_REQUEST)
