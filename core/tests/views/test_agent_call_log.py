from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from django.contrib.auth.hashers import make_password
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models.CustomUser import CustomUser as User
from user.models.agent_token import AgentToken
from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from django.utils import timezone

class AgentCallLogViewTestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme Corp", subdomain="acme")
        
        # 1. Create Standard User
        self.standard_user = User.objects.create_user(
            username="alex", 
            email="alex@example.com", 
            password="password",
            user_type=User.UserType.USER
        )
        TenantUserMapping.objects.create(user=self.standard_user, tenant=self.tenant)
        
        # 2. Create Voicebot Agent User
        self.vb_user = User.objects.create_user(
            username="voicebot_system",
            email="vb@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT
        )

        # 3. Create Agent Token for Voicebot
        self.secret = "my_super_secret"
        hashed_key = make_password(self.secret)
        self.prefix = "vb_test123"
        self.agent_token = AgentToken.objects.create(
            user=self.vb_user,
            name="Test Token",
            prefix=self.prefix,
            key=hashed_key,
            never_expires=True
        )
        self.raw_token = f"{self.prefix}_{self.secret}"

        # 4. Create an Agent profile
        self.agent = Agent.objects.create(
            tenant=self.tenant,
            bot_name="TestBot",
            persona_name="Tester",
            tone=Agent.ToneChoices.PROFESSIONAL,
            created_by=self.standard_user
        )

        # 5. Create Clients
        from rest_framework_simplejwt.tokens import RefreshToken
        from user.models.user_session import UserSession
        
        # Generate an actual JWT for the standard user to test the DRF auth pipeline properly
        refresh = RefreshToken.for_user(self.standard_user)
        access_token = refresh.access_token
        self.jwt_token = str(access_token)
        
        # Register the session in the database so CachedJWTAuthentication passes
        UserSession.objects.create(
            user=self.standard_user,
            access_jti=access_token.get("jti"),
            refresh_jti="dummy_refresh_jti",
            device_name="Test Client",
            ip_address="127.0.0.1",
            expires_at=timezone.now() + timezone.timedelta(days=1)
        )
        
        self.client_standard = APIClient()
        self.client_standard.credentials(HTTP_AUTHORIZATION=f"Bearer {self.jwt_token}")

        self.client_vb = APIClient()
        # X-Tenant-ID is mandatory on every endpoint an agent token can reach; without
        # it these requests are a 400 regardless of what they are testing.
        self.client_vb.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.raw_token}",
            HTTP_X_TENANT_ID=str(self.tenant.id),
        )

    def test_standard_user_cannot_create_log(self):
        payload = {
            "provider_call_id": "call-111",
            "agent": str(self.agent.id),
            "caller_number": "+1234567890",
            "direction": "inbound",
            "duration_seconds": 120,
            "outcome": "completed",
            "started_at": timezone.now().isoformat()
        }
        response = self.client_standard.post("/api/v1/core/agent-call-logs/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        
    def test_vb_agent_can_create_log(self):
        payload = {
            "provider_call_id": "call-222",
            "agent": str(self.agent.id),
            "caller_number": "+1987654321",
            "direction": "outbound",
            "duration_seconds": 60,
            "outcome": "voicemail",
            "started_at": timezone.now().isoformat(),
            "transcript": [{"speaker": "bot", "text": "Hello"}],
            "tags": ["test"]
        }
        response = self.client_vb.post("/api/v1/core/agent-call-logs/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(AgentCallLog.objects.count(), 1)
        self.assertEqual(AgentCallLog.objects.first().provider_call_id, "call-222")

    def test_invalid_agent_token_rejected(self):
        client_fake = APIClient()
        client_fake.credentials(HTTP_AUTHORIZATION=f"Bearer {self.prefix}_wrongsecret")
        
        response = client_fake.get("/api/v1/core/agent-call-logs/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["message"], "Invalid agent token secret.")

    def test_vb_agent_global_read_access(self):
        # Create a log under the tenant
        AgentCallLog.objects.create(
            tenant=self.tenant,
            provider_call_id="call-333",
            agent=self.agent,
            caller_number="+111",
            direction="inbound",
            duration_seconds=10,
            outcome="test",
            started_at=timezone.now()
        )
        
        # Test listing
        response = self.client_vb.get("/api/v1/core/agent-call-logs/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["provider_call_id"], "call-333")

    def test_standard_user_can_list_via_jwt(self):
        AgentCallLog.objects.create(
            tenant=self.tenant,
            provider_call_id="call-444",
            agent=self.agent,
            caller_number="+222",
            direction="inbound",
            duration_seconds=15,
            outcome="test_jwt",
            started_at=timezone.now()
        )
        # Test listing with JWT
        response = self.client_standard.get("/api/v1/core/agent-call-logs/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify DRF allowed the request to pass using JWT Authentication
        self.assertTrue(len(response.data["data"]) >= 1)
