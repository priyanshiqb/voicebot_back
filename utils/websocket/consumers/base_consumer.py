import json
import asyncio
import uuid
from urllib.parse import parse_qs
from channels.generic.websocket import AsyncWebsocketConsumer
from core.constants.ws_errors import WSErrors
from utils.websocket.group_builder import GroupNameBuilder
from main.settings import logger

class BaseConsumer(AsyncWebsocketConsumer):
    """
    Base generic WebSocket Consumer for normal (global) users.
    Assumes connection is authenticated via JWTAuthMiddleware (populating self.scope['user']).
    """
    group_registry = None

    async def connect(self):
        self.group_name = None
        self.is_accepted = False
        self.tab_id = None
        self.session = None

        # Extract user and session from scope (injected by JWT Auth Middleware)
        self.user = self.scope.get("user")
        self.session = self.scope.get("session")
        if not self.user or getattr(self.user, "is_anonymous", True) or not self.user.is_active:
            code, message = WSErrors.UNAUTHENTICATED
            await self.close_with_error(message, code)
            return

        # Parse or generate tab_id
        await self.assign_tab_id()

        self.group_name = await self.get_group_name()
        if not self.group_name:
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        # Add to tracking groups
        await self.add_to_tracking_groups()

        if not self.is_accepted:
            await self.safe_accept()
        await self.on_connect()

    async def assign_tab_id(self):
        """
        Parses or generates a tab_id from query parameters.
        """
        query_string = self.scope.get("query_string", b"").decode("utf-8")
        query_params = parse_qs(query_string)
        tab_id_param = query_params.get("tab_id", [None])[0]
        self.tab_id = tab_id_param if tab_id_param else str(uuid.uuid4())

    async def add_to_tracking_groups(self):
        """
        Adds the channel to user, session and tab specific groups for targeted broadcasting.
        """
        if self.user:
            await self.channel_layer.group_add(f"user_{self.user.id}", self.channel_name)
            
        if self.session:
            await self.channel_layer.group_add(f"session_{self.session.id}", self.channel_name)
            
        if self.tab_id:
            await self.channel_layer.group_add(f"tab_{self.tab_id}", self.channel_name)

    async def remove_from_tracking_groups(self):
        """
        Removes the channel from user, session and tab specific groups.
        """
        if self.user:
            await self.channel_layer.group_discard(f"user_{self.user.id}", self.channel_name)
            
        if self.session:
            await self.channel_layer.group_discard(f"session_{self.session.id}", self.channel_name)
            
        if self.tab_id:
            await self.channel_layer.group_discard(f"tab_{self.tab_id}", self.channel_name)

    async def get_group_name(self):
        """
        Builds group name dynamically using the provided group_registry.
        Override in child if custom group logic is needed.
        """
        if not self.group_registry:
            code, message = WSErrors.GROUP_REGISTRY_NOT_DEFINED
            await self.close_with_error(message, code)
            return None 

        try:
            return GroupNameBuilder.build(
                group_config=self.group_registry,
                user_id=self.user.id
            )
        except ValueError as e:
            logger.error(f"Group build error: {e}")
            code, message = WSErrors.INTERNAL_ERROR
            await self.close_with_error(str(e), code)
            return None

    async def safe_accept(self):
        if not self.is_accepted:
            await self.accept()
            self.is_accepted = True

    async def on_connect(self):
        await self.send_update(
            event_type="connection.established",
            message="Connected successfully",
            data={
                "group": self.group_name,
                "user_id": str(self.user.id) if self.user else None,
                "tab_id": self.tab_id,
                "session_id": str(self.session.id) if self.session else None
            }
        )

    async def disconnect(self, close_code):
        if self.group_name:
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
            
        await self.remove_from_tracking_groups()

    async def receive(self, text_data=None, bytes_data=None):
        pass

    async def close_with_error(self, message, code):
        if not self.is_accepted:
            await self.safe_accept()

        await self.send_update(
            event_type="connection.err_close",
            message=message
        )

        await asyncio.sleep(0.1)
        await self.close(code=code)

    async def send_update(self, event=None, event_type="update", data=None, message=None):
        try:
            payload = {
                "type": event_type,
            }

            if data is None and event:
                data = event.get("data", {})

            if data is not None:
                payload["data"] = data

            if message:
                payload["message"] = message

            await self.send(text_data=json.dumps(payload))

        except Exception as e:
            logger.error(f"Error sending update: {e}")
