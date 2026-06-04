# utils/exceptions.py

from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
import logging

logger = logging.getLogger("django")  # or your custom logger


from rest_framework.exceptions import APIException


class OnboardingPendingException(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Your registration is still pending. Please complete your onboarding."
    default_code = "onboarding_pending"

    def __init__(self, temp_registration_id, email, detail=None, code=None):
        super().__init__(detail, code)
        self.temp_registration_id = temp_registration_id
        self.email = email


def custom_exception_handler(exc, context):
    request = context.get("request")
    request_id = getattr(request, "request_id", "N/A") if request else "N/A"
    log_extra = {"request_id": request_id}

    if isinstance(exc, OnboardingPendingException):
        logger.warning(
            "Onboarding pending redirection",
            extra={
                "detail": exc.detail,
                "temp_registration_id": exc.temp_registration_id,
                **log_extra
            }
        )
        return Response({
            "status": False,
            "message": str(exc.detail),
            "data": None,
            "errors": {
                "onboarding_pending": True,
                "temp_registration_id": exc.temp_registration_id,
                "email": exc.email
            },
            "request_id": request_id
        }, status=status.HTTP_400_BAD_REQUEST)

    response = exception_handler(exc, context)
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