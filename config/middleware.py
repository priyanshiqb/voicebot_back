import uuid
import time
import logging
from django.conf import settings
from utils.logging.context import set_log_context, clear_log_context
from utils.logging.helper_functions import _get_extra, _get_client_ip

logger = logging.getLogger("django")

class RequestTracingMiddleware:
    """
    Enterprise telemetry middleware that:
    1. Generates or extracts a unique X-Request-ID for every incoming request.
    2. Stores request metadata in thread/async-safe log context.
    3. Measures exact request execution duration in milliseconds.
    4. Appends the request ID to the response headers.
    5. Cleans up context after execution to prevent memory leaks.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 1. Trace/generate Request ID
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            request_id = str(uuid.uuid4())
        
        request.request_id = request_id
        
        # 2. Extract base request metadata for log context (global tracing ID only)
        context_data = {
            "request_id": request_id,
        }
        
        # Clear context first to prevent any pollution from previous executions
        clear_log_context()
        set_log_context(context_data)
        
        # 3. Track execution duration
        start_time = time.monotonic()
        
        try:
            response = self.get_response(request)
            
            # Update context with user info if authenticated during the request
            if hasattr(request, "user") and request.user and request.user.is_authenticated:
                context_data["user_id"] = str(request.user.id)
                try:
                    context_data["tenant_id"] = str(request.user.tenant_mapping.tenant.id)
                except Exception:
                    pass
                set_log_context(context_data)
                
            duration_ms = int((time.monotonic() - start_time) * 1000)
            
            # 4. Inject tracing ID into response header
            response["X-Request-ID"] = request_id
            
            # 5. Log request completion using full request/response telemetry helper
            log_message = (
                f"Request completed: {request.method} {request.path} -> "
                f"Status {response.status_code} in {duration_ms}ms"
            )
            logger.info(
                log_message,
                extra=_get_extra(
                    request=request,
                    response=response,
                    add_dict={"duration_ms": duration_ms}
                )
            )
            return response
        finally:
            # 6. Always clear context to avoid variables leaking into other requests
            clear_log_context()



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


