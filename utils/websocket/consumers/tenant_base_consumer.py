from channels.db import database_sync_to_async
from core.constants.ws_errors import WSErrors
from utils.websocket.group_builder import GroupNameBuilder
from utils.websocket.consumers.base_consumer import BaseConsumer
from onboard.models.tenant.tenant_model import Tenant
from main.settings import logger

class TenantBaseConsumer(BaseConsumer):
    """
    WebSocket Consumer for tenant-scoped connections.
    Requires both a valid user authentication AND a valid tenant_id in the URL route.
    """
    
    async def connect(self):
        self.group_name = None
        self.is_accepted = False
        self.tab_id = None
        self.session = None

        # 1. Authenticate user
        self.user = self.scope.get("user")
        self.session = self.scope.get("session")
        if not self.user or getattr(self.user, "is_anonymous", True) or not self.user.is_active:
            code, message = WSErrors.UNAUTHENTICATED
            await self.close_with_error(message, code)
            return

        # 2. Extract tenant ID strictly from the User's database mapping
        self.tenant_id = await self._extract_tenant_from_user()
        if not self.tenant_id:
            logger.error(f"User {self.user.id} has no associated tenant mapping.")
            code, message = WSErrors.INVALID_TENANT
            await self.close_with_error(message, code)
            return
            
        # Parse or generate tab_id from query params
        await self.assign_tab_id()
    
        # 4. Generate Group Name
        self.group_name = await self.get_group_name()
        if not self.group_name:
            return

        # 5. Accept and Add to Channel Layer Group
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        # Add to tracking groups
        await self.add_to_tracking_groups()

        if not self.is_accepted:
            await self.safe_accept()
            
        await self.on_connect()

    async def add_to_tracking_groups(self):
        """
        Adds the channel to user, session, tab AND tenant specific groups.
        """
        await super().add_to_tracking_groups()
        await self.channel_layer.group_add(f"tenant_{self.tenant_id}", self.channel_name)

    async def remove_from_tracking_groups(self):
        """
        Removes the channel from tracking groups including the tenant group.
        """
        await super().remove_from_tracking_groups()
        await self.channel_layer.group_discard(f"tenant_{self.tenant_id}", self.channel_name)

    async def get_group_name(self):
        """
        Builds the group name with tenant_id and user_id context.
        """
        if not self.group_registry:
            code, message = WSErrors.GROUP_REGISTRY_NOT_DEFINED
            await self.close_with_error(message, code)
            return None 

        try:
            return GroupNameBuilder.build(
                group_config=self.group_registry,
                tenant_id=self.tenant_id,
                user_id=self.user.id
            )
        except ValueError as e:
            logger.error(f"Group build error: {e}")
            code, message = WSErrors.INTERNAL_ERROR
            await self.close_with_error(str(e), code)
            return None

    async def on_connect(self):
        await self.send_update(
            event_type="connection.established",
            message="Connected successfully",
            data={
                "group": self.group_name,
                "tenant_id": str(self.tenant_id),
                "user_id": str(self.user.id) if self.user else None,
                "tab_id": self.tab_id,
                "session_id": str(self.session.id) if self.session else None
            }
        )

    @database_sync_to_async
    def _extract_tenant_from_user(self):
        """
        Extracts the tenant ID directly from the authenticated user's profile.
        This inherently enforces security by ignoring any client-provided IDs.
        """
        from tenant.models.TenantUserMapping import TenantUserMapping
        mapping = TenantUserMapping.objects.filter(user=self.user).first()
        return str(mapping.tenant_id) if mapping else None
