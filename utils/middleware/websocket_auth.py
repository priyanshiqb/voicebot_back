import json
from channels.middleware import BaseMiddleware
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from urllib.parse import parse_qs

User = get_user_model()


@database_sync_to_async
def get_user_from_token(token_string):
    """
    Extracts user ID from SimpleJWT access token and validates the active user.
    Returns (user, session, None) if successful, or (None, None, error_message) if authentication fails.
    """
    try:
        token = AccessToken(token_string)
        user_id = token.payload.get("user_id")
        jti = token.payload.get("jti")
        
        if not user_id:
            return None, None, "Invalid token payload: user_id not found"
        
        try:
            user = User.objects.get(id=user_id, is_active=True)
            
            from user.models.user_session import UserSession
            try:
                # get_active_session, not validate_session_jti: the row itself goes
                # into the connection scope so consumers can address one session,
                # and the validate path may answer from cache and return None. A
                # handshake happens once per connection, so the read is free.
                session = UserSession.get_active_session(jti)
                return user, session, None
            except UserSession.DoesNotExist:
                return None, None, "Session is invalid or logged out"
                
        except User.DoesNotExist:
            return None, None, "User not found or is inactive"
            
    except TokenError as e:
        # Catches Expired or Invalid signature errors directly from SimpleJWT
        return None, None, str(e) or "Token is invalid or expired"
    except Exception:
        return None, None, "Authentication failed"


class JWTAuthMiddleware(BaseMiddleware):
    """
    Production-grade B2B Multi-Tenant WebSocket Security Middleware.
    
    Extracts token from:
    1. HTTP 'Authorization: Bearer <TOKEN>' headers
    2. 'Sec-WebSocket-Protocol' subprotocol headers
    3. URL Query parameters (?token=<TOKEN>)
    
    If authentication fails for any reason (missing, expired, invalid token, or inactive user),
    it immediately accepts, transmits a clean JSON error response, and closes the connection.
    """
    async def __call__(self, scope, receive, send):
        token_string = None

        # Extract headers dict from the connection scope
        headers = dict(scope.get("headers", []))

        # Path 1: Check standard HTTP 'Authorization' header
        auth_header = headers.get(b"authorization")
        if auth_header:
            try:
                auth_str = auth_header.decode("utf-8")
                if auth_str.startswith("Bearer "):
                    token_string = auth_str.split(" ")[1]
            except Exception:
                pass

        # Path 2: Check standard browser 'Sec-WebSocket-Protocol' header (often used as token header in web browsers)
        if not token_string:
            sec_proto = headers.get(b"sec-websocket-protocol")
            if sec_proto:
                try:
                    sec_proto_str = sec_proto.decode("utf-8")
                    # Usually formatted as 'Bearer, <TOKEN>' or '<TOKEN>'
                    parts = [p.strip() for p in sec_proto_str.split(",")]
                    for part in parts:
                        if part != "Bearer" and part != "":
                            token_string = part
                            break
                except Exception:
                    pass

        # Path 3: Fallback to checking URL Query String parameters
        if not token_string:
            query_string = scope.get("query_string", b"").decode("utf-8")
            query_params = parse_qs(query_string)
            token = query_params.get("token")
            if token:
                token_string = token[0]

        # Enforce token existence
        if not token_string:
            await self.close_with_error(send, "Token not found")
            return

        # Validate and authenticate user
        user, session, error_message = await get_user_from_token(token_string)
        if error_message:
            await self.close_with_error(send, error_message)
            return

        # Authentication succeeded! Assign user and session to connection scope
        scope["user"] = user
        scope["session"] = session
        return await super().__call__(scope, receive, send)

    async def close_with_error(self, send, message):
        """
        Sends an error message and closes the WebSocket connection gracefully.
        """
        await send({
            "type": "websocket.accept"
        })
        await send({
            "type": "websocket.send",
            "text": json.dumps({
                "error": True,
                "message": message
            })
        })
        await send({
            "type": "websocket.close"
        })
