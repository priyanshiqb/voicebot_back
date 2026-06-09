import uuid
import time
import logging
from django.conf import settings

logger = logging.getLogger("django")

class RequestTracingMiddleware:
    """
    Enterprise telemetry middleware that:
    1. Generates or extracts a unique X-Request-ID for every incoming request.
    2. Measures exact request execution duration in milliseconds.
    3. Appends the request ID to the response headers.
    4. Logs structured data about request completion.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 1. Trace/generate Request ID
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            request_id = str(uuid.uuid4())
        
        # Annotate the request object so that custom logging formatters 
        # and standard exception handlers can display it dynamically.
        request.request_id = request_id
        
        # 2. Track execution duration
        start_time = time.monotonic()
        
        response = self.get_response(request)
        
        duration_ms = int((time.monotonic() - start_time) * 1000)
        
        # 3. Inject tracing ID into response header
        response["X-Request-ID"] = request_id
        
        # 4. Structured logging output
        log_message = (
            f"Request completed: {request.method} {request.path} -> "
            f"Status {response.status_code} in {duration_ms}ms"
        )
        
        # Inject fields if logger has support
        logger.info(
            log_message,
            extra={
                "fields_name": ["request_id", "duration_ms", "status_code"],
                "request_id": request_id,
                "duration_ms": duration_ms,
                "status_code": response.status_code,
            }
        )
        
        return response


class ReCaptchaMiddleware:
    """
    Middleware to verify the X-ReCaptcha-Token header for a specific list of API requests.
    Only active if settings.RECAPTCHA_VERIFICATION_ON is True.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Only validate if the feature is toggled ON in settings
        if getattr(settings, "RECAPTCHA_VERIFICATION_ON", False):
            path = request.path
            required_paths = getattr(settings, "RECAPTCHA_REQUIRED_PATHS", [])
            
            if path in required_paths:
                # Fetch x-recaptcha-token from header (case-insensitive lookup)
                recaptcha_token = request.headers.get("x-recaptcha-token")
                
                # As of now, we just fetch it and return True (let request proceed)
                # Later we will implement how we will verify the token.
                pass

        return self.get_response(request)


