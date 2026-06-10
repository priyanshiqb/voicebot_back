import asyncio
import json
from django.test import TransactionTestCase
from django.contrib.auth import get_user_model
from django.urls import re_path
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from channels.db import database_sync_to_async
from rest_framework.test import APIClient

from core.constants.ws_errors import WSErrors
from utils.websocket.consumers.base_consumer import BaseConsumer
from utils.websocket.consumers.tenant_base_consumer import TenantBaseConsumer
from utils.websocket.group_builder import GroupNameBuilder
from utils.middleware.websocket_auth import JWTAuthMiddleware

User = get_user_model()

# Define test consumers
class TestBaseConsumer(BaseConsumer):
    group_registry = {
        "prefix": "test_global",
        "context": ["user_id"]
    }

class TestBaseConsumerNoRegistry(BaseConsumer):
    group_registry = None

class TestTenantConsumer(TenantBaseConsumer):
    group_registry = {
        "prefix": "test_tenant",
        "context": ["tenant_id", "user_id"]
    }

# Define test routing stack
test_application = JWTAuthMiddleware(
    URLRouter([
        re_path(r"^ws/test-base/$", TestBaseConsumer.as_asgi()),
        re_path(r"^ws/test-base-no-reg/$", TestBaseConsumerNoRegistry.as_asgi()),
        re_path(r"^ws/test-tenant/$", TestTenantConsumer.as_asgi()),
    ])
)


class WebSocketConsumerTests(TransactionTestCase):
    @database_sync_to_async
    def create_user_and_session_sync(self):
        user = User.objects.create_user(
            email="test.ws@example.com",
            password="SecurePass123!",
            first_name="Test",
            last_name="WS"
        )
        user.is_active = True
        user.save()
        
        # Authenticate using standard Django REST framework login endpoint
        client = APIClient()
        response = client.post(
            "/api/v1/auth/login/",
            {"email": "test.ws@example.com", "password": "SecurePass123!"},
            format="json"
        )
        token = response.data["data"]["access"]
        
        from user.models.user_session import UserSession
        session = UserSession.objects.get(user=user, is_active=True)
        self.assertIsNotNone(session.access_iat)
        self.assertIsNotNone(session.access_exp)
        self.assertIsNotNone(session.refresh_iat)
        self.assertIsNotNone(session.refresh_exp)
        return user, token, session

    @database_sync_to_async
    def create_tenant_and_user_mapping_sync(self, user):
        from tenant.models import Tenant, TenantUserMapping
        from onboard.models.timezone import Timezone
        from onboard.models.language import Language
        
        timezone_obj, _ = Timezone.objects.get_or_create(
            timezone="UTC",
            defaults={"offset": "00:00", "region": "UTC"}
        )
        language_obj, _ = Language.objects.get_or_create(
            code="en-IN",
            defaults={"name": "English"}
        )
        
        tenant = Tenant.objects.create(
            name="Test Tenant",
            subdomain="test-tenant-ws",
            timezone=timezone_obj,
            language=language_obj
        )
        mapping = TenantUserMapping.objects.create(
            user=user,
            tenant=tenant
        )
        return tenant, mapping

    async def test_group_name_builder(self):
        """
        Verify that GroupNameBuilder correctly validates inputs and builds group names.
        """
        config = {"prefix": "test_prefix", "context": ["tenant_id", "user_id"], "separator": ":"}
        name = GroupNameBuilder.build(config, tenant_id="t123", user_id="u456")
        self.assertEqual(name, "test_prefix:t123:u456")

        # Test fallback separator
        config_no_sep = {"prefix": "test_prefix", "context": ["user_id"]}
        name_fallback = GroupNameBuilder.build(config_no_sep, user_id="u789")
        self.assertEqual(name_fallback, "test_prefix_u789")

        # Missing prefix
        with self.assertRaises(ValueError):
            GroupNameBuilder.build({"context": ["user_id"]}, user_id="u123")

        # Missing required context
        with self.assertRaises(ValueError):
            GroupNameBuilder.build(config_no_sep)

    async def test_base_consumer_no_token_failure(self):
        """
        Verify that connecting without a JWT token leads to connection close by middleware.
        """
        communicator = WebsocketCommunicator(test_application, "/ws/test-base/")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)  # Middleware accepts to send error payload

        response = await communicator.receive_json_from()
        self.assertTrue(response.get("error"))
        self.assertEqual(response.get("message"), "Token not found")
        
        # Connection should close
        await communicator.wait()
        await communicator.disconnect()

    async def test_base_consumer_success(self):
        """
        Verify that a valid authenticated connection succeeds and joins correct groups.
        """
        user, token, session = await self.create_user_and_session_sync()
        communicator = WebsocketCommunicator(test_application, f"/ws/test-base/?token={token}")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        response = await communicator.receive_json_from()
        self.assertEqual(response.get("type"), "connection.established")
        self.assertEqual(response["data"]["user_id"], str(user.id))
        self.assertEqual(response["data"]["session_id"], str(session.id))
        self.assertEqual(response["data"]["group"], f"test_global_{user.id}")

        await communicator.disconnect()

    async def test_base_consumer_missing_registry(self):
        """
        Verify that a consumer missing group_registry config closes with the proper error code.
        """
        user, token, session = await self.create_user_and_session_sync()
        communicator = WebsocketCommunicator(test_application, f"/ws/test-base-no-reg/?token={token}")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        response = await communicator.receive_json_from()
        self.assertEqual(response.get("type"), "connection.err_close")
        self.assertIn("missing", response.get("message", "").lower())

        await communicator.disconnect()

    async def test_tenant_consumer_missing_tenant_mapping(self):
        """
        Verify that a user without a tenant user mapping is disconnected with INVALID_TENANT.
        """
        user, token, session = await self.create_user_and_session_sync()
        communicator = WebsocketCommunicator(test_application, f"/ws/test-tenant/?token={token}")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        response = await communicator.receive_json_from()
        self.assertEqual(response.get("type"), "connection.err_close")
        self.assertEqual(response.get("message"), WSErrors.INVALID_TENANT[1])

        await communicator.disconnect()

    async def test_tenant_consumer_success(self):
        """
        Verify that an authenticated user with valid tenant mapping connects successfully.
        """
        user, token, session = await self.create_user_and_session_sync()
        tenant, mapping = await self.create_tenant_and_user_mapping_sync(user)

        communicator = WebsocketCommunicator(test_application, f"/ws/test-tenant/?token={token}")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        response = await communicator.receive_json_from()
        self.assertEqual(response.get("type"), "connection.established")
        self.assertEqual(response["data"]["tenant_id"], str(tenant.id))
        self.assertEqual(response["data"]["group"], f"test_tenant_{tenant.id}_{user.id}")

        await communicator.disconnect()

    @database_sync_to_async
    def set_cache_revoked_sync(self, jti):
        from django.core.cache import cache
        cache.set(f"user_session:access:{jti}", "revoked", timeout=300)

    async def test_websocket_caching(self):
        """
        Verify that if JTI is cached as revoked in Redis, the connection is rejected immediately.
        """
        user, token, session = await self.create_user_and_session_sync()
        
        # Set cache key to revoked
        await self.set_cache_revoked_sync(session.access_jti)
        
        from unittest.mock import patch
        from user.models.user_session import UserSession
        
        with patch.object(UserSession.objects, "get") as mock_get:
            communicator = WebsocketCommunicator(test_application, f"/ws/test-base/?token={token}")
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            
            response = await communicator.receive_json_from()
            self.assertTrue(response.get("error"))
            self.assertEqual(response.get("message"), "Session is invalid or logged out")
            
            # Verify that the database was not queried for the session!
            mock_get.assert_not_called()
            
            await communicator.wait()
            await communicator.disconnect()

    async def test_websocket_redis_down(self):
        """
        Verify that if Redis cache raises errors (downtime), the connection still succeeds by falling back to DB.
        """
        user, token, session = await self.create_user_and_session_sync()
        
        from unittest.mock import patch
        from django.core.cache import cache
        
        # Mock cache.get and cache.set to raise exception
        def mock_error(*args, **kwargs):
            raise Exception("Redis connection refused")
            
        with patch.object(cache, "get", side_effect=mock_error), \
             patch.object(cache, "set", side_effect=mock_error):
             
            communicator = WebsocketCommunicator(test_application, f"/ws/test-base/?token={token}")
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            
            response = await communicator.receive_json_from()
            self.assertEqual(response.get("type"), "connection.established")
            
            await communicator.disconnect()
