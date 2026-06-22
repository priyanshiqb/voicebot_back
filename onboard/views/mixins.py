from rest_framework.response import Response
from rest_framework import status
from django.db import transaction, connection
from django.db.utils import OperationalError
import logging

logger = logging.getLogger(__name__)

class QueryTimeoutException(Exception):
    pass

class GeographyTimeoutMixin:
    """
    Mixin to enforce a strict PostgreSQL database statement timeout on API views.
    It wraps the entire DRF dispatch lifecycle inside a database transaction
    and sets a session-level timeout. If the query exceeds this time, it 
    gracefully catches the OperationalError and returns a 408 Request Timeout.
    """
    query_timeout_ms = 5000  # Default 5 seconds

    def dispatch(self, request, *args, **kwargs):
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    # Enforce timeout only if the active database is PostgreSQL
                    if connection.vendor == 'postgresql':
                        cursor.execute(f"SET LOCAL statement_timeout = {self.query_timeout_ms};")
                
                # Execute standard DRF dispatch
                return super().dispatch(request, *args, **kwargs)
                
        except OperationalError as e:
            # Catch PostgreSQL statement timeout exceptions
            if 'statement timeout' in str(e).lower() or 'query_canceled' in str(e).lower():
                logger.error(f"Geography API query timeout ({self.query_timeout_ms}ms) exceeded in {self.__class__.__name__}")
                return Response({
                    "status": False,
                    "message": "Query timeout exceeded. Please refine your search parameters.",
                    "data": None,
                    "errors": {"timeout": "The requested data took too long to fetch."}
                }, status=status.HTTP_408_REQUEST_TIMEOUT)
            
            # Re-raise if it's a different database OperationalError
            raise
