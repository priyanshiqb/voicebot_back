import re

def parse_user_agent(user_agent_string):
    """
    Lightweight, dependency-free utility to parse User-Agent header into browser and OS.
    """
    if not user_agent_string:
        return "Unknown Device"
    
    ua = user_agent_string.lower()
    
    # Detect Browser / Client
    if "postman" in ua:
        browser = "Postman"
    elif "chrome" in ua or "chromium" in ua:
        if "edg" in ua:
            browser = "Edge"
        elif "opr" in ua or "opera" in ua:
            browser = "Opera"
        else:
            browser = "Chrome"
    elif "safari" in ua and "chrome" not in ua:
        browser = "Safari"
    elif "firefox" in ua:
        browser = "Firefox"
    else:
        browser = "Browser/Client"

    # Detect OS
    if "windows" in ua:
        os_name = "Windows"
    elif "macintosh" in ua or "mac os" in ua:
        os_name = "macOS"
    elif "linux" in ua:
        if "android" in ua:
            os_name = "Android"
        else:
            os_name = "Linux"
    elif "iphone" in ua or "ipad" in ua or "ipod" in ua:
        os_name = "iOS"
    else:
        os_name = "Unknown OS"

    return f"{browser} on {os_name}"


def get_client_ip(request):
    """
    Extract client IP from request META.
    """
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip
