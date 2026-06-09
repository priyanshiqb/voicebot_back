class WSErrors:
    """
    Standardized WebSocket Error Codes and Messages.
    Code range starts at 4000 (valid custom WebSocket close codes).
    """
    MISSING_TENANT_ID = (4000, "Missing tenant_id in connection route")
    INVALID_TENANT = (4001, "Invalid or unauthorized tenant")
    GROUP_REGISTRY_NOT_DEFINED = (4002, "Group registry configuration is missing")
    UNAUTHENTICATED = (4003, "Authentication failed or token missing")
    INTERNAL_ERROR = (4004, "Internal server error")
