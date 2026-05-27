# utils/exceptions.py

from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
import logging

logger = logging.getLogger("django")  # or your custom logger


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    request = context.get("request")
    request_id = getattr(request, "request_id", "N/A") if request else "N/A"
    log_extra = {"request_id": request_id}

    if response is not None:
        status_code = response.status_code

        # Logging strategy
        if status_code >= 500:
            logger.exception("Server error", exc_info=exc, extra=log_extra)
        else:
            logger.warning(
                "Client error",
                extra={
                    "detail": response.data,
                    **log_extra
                }
            )

        return Response({
            "status": False,
            "message": _get_error_message(response),
            "data": None,
            "errors": response.data if isinstance(response.data, dict) else None,
            "request_id": request_id
        }, status=status_code)

    logger.exception("Unhandled exception", exc_info=exc, extra=log_extra)

    return Response({
        "status": False,
        "message": "Something went wrong",
        "data": None,
        "errors": None,
        "request_id": request_id
    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _get_error_message(response):
    data = response.data

    # Case 1: Standard DRF "detail"
    if isinstance(data, dict) and "detail" in data:
        return data["detail"]

    # Case 2: non_field_errors (common in DRF)
    if isinstance(data, dict) and "non_field_errors" in data:
        return data["non_field_errors"][0]

    # Case 3: Field errors (dict)
    if isinstance(data, dict):
        first_key = next(iter(data))
        first_error = data[first_key]

        if isinstance(first_error, list):
            return first_error[0]
        return str(first_error)

    # Case 4: List errors
    if isinstance(data, list) and data:
        return data[0]

    # Fallback
    return "Something went wrong"