import json
from django.conf import settings
import re


def _get_extra(request=None, response=None,add_dict=None):
    """A private helper to build the context dictionary from a request."""
    data = {}

    if request is not None:

        data['req_id'] = getattr(request, "request_id", 'N/A')
        data['req_path'] = getattr(request, 'path', 'N/A')
        data['req_method'] = getattr(request, 'method', 'N/A')
        data['req_query_params'] = dict(request.GET)
        data['req_user_agent'] = request.META.get('HTTP_USER_AGENT', '')
        data['req_ip_address'] = _get_client_ip(request)
        # data['req_headers'] = _get_req_headers(request)

        if hasattr(request, "user") and request.user and request.user.is_authenticated:
            data['req_user_id'] = str(request.user.id)
            data['req_user_tenant'] = 'N/A'

            try:
                data['req_user_tenant'] = str(request.user.tenant_mapping.tenant.id)
            except Exception as e:
                data['req_user_tenant'] = 'N/A'

        else:
            data['req_user'] = 'Anonymous'
            data['req_user_id'] = 'N/A'

        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            try:
                body = _get_request_body(request)
                data['req_body'] = _sanitize_data(body)
            except Exception as e:
                data['req_body_error'] = str(e)
    if response is not None:
        data['res_code'] = getattr(response, 'status_code', 'N/A')
        data['res_size_bytes'] = len(response.content) if hasattr(response, 'content') else 0

    if add_dict is not None:
        for key, value in add_dict.items():
            data[key] = value

    return data




def _get_req_headers(request):
    """Extract request headers"""
    SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key"}
    return {
        h: v
        for h, v in request.headers.items()
        if h.lower() not in SENSITIVE_HEADERS
    }


def _get_client_ip(request):
    """Extract client IP address, handling proxies"""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def _get_request_body(request):
    """Safely extract request body"""
    # For Django REST framework requests
    if hasattr(request, '_cached_body'):
        try:
            body = request.body
            if body:
                try:
                    return json.loads(body.decode('utf-8'))
                except json.JSONDecodeError:
                    return request.body.decode('utf-8')[:1000]
        except Exception as e:
            return f"<error extracting request body: {e}>"
    return {}


sensitive_fields = getattr(settings, 'SENSITIVE_FIELDS', ['password', 'token', 'secret', 'key'])
sensitive_pattern = re.compile(
    '|'.join(sensitive_fields),
    re.IGNORECASE
)


def _sanitize_data(data):
    """Recursively sanitize data by removing sensitive fields"""
    if isinstance(data, dict):
        sanitized = {}
        for key, value in data.items():
            if sensitive_pattern.search(key):
                sanitized[key] = '***REDACTED***'
            else:
                sanitized[key] = _sanitize_data(value)
        return sanitized
    elif isinstance(data, list):
        return [_sanitize_data(item) for item in data]
    else:
        return data
