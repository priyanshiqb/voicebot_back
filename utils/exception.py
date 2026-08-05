# utils/exceptions.py

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
import logging

logger = logging.getLogger("django")  # or your custom logger


from rest_framework.exceptions import APIException
from rest_framework.exceptions import ValidationError as DRFValidationError


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

    # Django's ValidationError is not a DRF exception, so DRF's handler ignores it
    # and it would fall through to the 500 branch below. Models raise it from
    # `full_clean()` and from guarded `soft_delete()` overrides — those are
    # deliberate, user-facing refusals with a written explanation, and returning
    # "Something went wrong" throws that explanation away and reads as a server
    # fault. Translate it to the DRF equivalent so it surfaces as a 400.
    if isinstance(exc, DjangoValidationError):
        exc = DRFValidationError(
            exc.message_dict if hasattr(exc, "message_dict") else exc.messages
        )

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