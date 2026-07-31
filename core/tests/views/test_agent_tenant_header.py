"""
`X-Tenant-ID` resolution and scoping for VB_AGENT callers.

Three things are pinned here:

1. How the header value itself is handled — malformed, unknown, soft-deleted, blank.
2. That a valid header scopes reads to that tenant, and that a write naming an agent in
   a different tenant fails rather than silently landing there.
3. That it does nothing for ordinary users. A human's scope comes from their tenant
   mapping, and a header they control must never widen or narrow it.

That the header is *mandatory* is covered separately, in
test_vb_agent_created_record_tenancy.HeaderIsMandatoryEverywhereTestCase, alongside the
records-land-in-the-right-tenant invariant it exists to serve.

Note this is a filter, not an authorization boundary — a token holder may name any
tenant. See tenant/common/views/agent_tenant_scope.py for why, and what would have
to change to make it one.
"""

from django.contrib.auth.hashers import make_password
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.agent import Agent
from core.models.agent_call_log import AgentCallLog
from core.models.campaign import Campaign
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models.CustomUser import CustomUser as User
from user.models.agent_token import AgentToken

AGENTS_URL = "/api/v1/core/agents/"
CALL_LOGS_URL = "/api/v1/core/agent-call-logs/"
RUNNER_URL = "/api/v1/core/campaign-runner/"


class AgentTenantHeaderTestCase(APITestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Acme", subdomain="acme-hdr")
        self.tenant_b = Tenant.objects.create(name="Globex", subdomain="globex-hdr")

        self.runner_user = User.objects.create_user(
            username="voicebot_hdr",
            email="vb-hdr@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT,
        )

        secret = "header_scope_secret"
        prefix = "vb_hdrtest"
        AgentToken.objects.create(
            user=self.runner_user,
            name="Header Test Token",
            prefix=prefix,
            key=make_password(secret),
            never_expires=True,
        )
        # Authenticated through the real token so the whole auth pipeline is
        # exercised, not just a forced user.
        self.runner = APIClient()
        self.runner.credentials(HTTP_AUTHORIZATION=f"Bearer {prefix}_{secret}")

        self.human = User.objects.create_user(
            username="alex_hdr",
            email="alex-hdr@example.com",
            password="password",
            user_type=User.UserType.USER,
        )
        TenantUserMapping.objects.create(user=self.human, tenant=self.tenant_a)
        self.human_client = APIClient()
        self.human_client.force_authenticate(user=self.human)

        self.agent_a = self._agent(self.tenant_a, "Bot A")
        self.agent_b = self._agent(self.tenant_b, "Bot B")

        self._call_log(self.tenant_a, self.agent_a, "call-a-1")
        self._call_log(self.tenant_b, self.agent_b, "call-b-1")

        self.campaign_b = Campaign.objects.create(
            tenant=self.tenant_b,
            agent=self.agent_b,
            name="Globex Campaign",
            status=Campaign.Status.RUNNING,
        )

    @staticmethod
    def _agent(tenant, bot_name):
        return Agent.objects.create(
            tenant=tenant,
            bot_name=bot_name,
            persona_name="Persona",
            languages=["en-IN"],
        )

    @staticmethod
    def _call_log(tenant, agent, provider_call_id):
        return AgentCallLog.objects.create(
            tenant=tenant,
            agent=agent,
            provider_call_id=provider_call_id,
            caller_number="+14155552671",
            direction=AgentCallLog.CallDirection.INBOUND,
        )

    @staticmethod
    def _ids(response):
        # SetPagination puts the serialized rows directly under "data"; there is no
        # nested "results" key.
        return {row["id"] for row in response.data["data"]}

    # ---- reads: the header scopes ----

    def test_a_token_can_still_reach_either_tenant_by_naming_it(self):
        """
        The header is a filter, not an authorization boundary. Nothing on the token
        restricts which tenant it may name, and this test says so out loud — if it ever
        starts failing, someone has added the real boundary (tenants on AgentToken) and
        the docs in agent_tenant_scope.py need updating with it.
        """
        for agent, tenant in ((self.agent_a, self.tenant_a), (self.agent_b, self.tenant_b)):
            with self.subTest(agent=agent.bot_name):
                response = self.runner.get(
                    f"{AGENTS_URL}{agent.id}/", HTTP_X_TENANT_ID=str(tenant.id)
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_agent_retrieve_scoped_by_header(self):
        own = self.runner.get(
            f"{AGENTS_URL}{self.agent_a.id}/", HTTP_X_TENANT_ID=str(self.tenant_a.id)
        )
        self.assertEqual(own.status_code, status.HTTP_200_OK)

        foreign = self.runner.get(
            f"{AGENTS_URL}{self.agent_b.id}/", HTTP_X_TENANT_ID=str(self.tenant_a.id)
        )
        self.assertEqual(foreign.status_code, status.HTTP_404_NOT_FOUND)

    def test_call_logs_scoped_by_header(self):
        response = self.runner.get(
            CALL_LOGS_URL, HTTP_X_TENANT_ID=str(self.tenant_a.id)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["data"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["provider_call_id"], "call-a-1")

    def test_runner_context_404s_for_campaign_outside_header_tenant(self):
        response = self.runner.get(
            f"{RUNNER_URL}{self.campaign_b.id}/context/",
            HTTP_X_TENANT_ID=str(self.tenant_a.id),
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_runner_members_update_404s_outside_header_tenant(self):
        response = self.runner.post(
            f"{RUNNER_URL}{self.campaign_b.id}/members/update/",
            {"updates": []},
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant_a.id),
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # ---- bad header values ----

    # Header parsing is checked on call logs: it is the same resolver everywhere, and
    # this endpoint still exposes `list`, so a bad header can be distinguished from an
    # empty result.

    def test_malformed_header_is_rejected(self):
        response = self.runner.get(CALL_LOGS_URL, HTTP_X_TENANT_ID="not-a-uuid")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_tenant_header_is_rejected(self):
        # A typo must surface as an error rather than as an empty list, which would
        # otherwise read as "this tenant has no call logs".
        response = self.runner.get(
            CALL_LOGS_URL, HTTP_X_TENANT_ID="00000000-0000-0000-0000-000000000000"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_soft_deleted_tenant_header_is_rejected(self):
        self.tenant_b.is_deleted = True
        self.tenant_b.save(update_fields=["is_deleted"])

        response = self.runner.get(
            CALL_LOGS_URL, HTTP_X_TENANT_ID=str(self.tenant_b.id)
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_blank_header_is_treated_as_absent(self):
        # Whitespace is absence, not a value — and absence is now an error, so a client
        # sending an unset variable gets told rather than silently scoped to nothing.
        response = self.runner.get(CALL_LOGS_URL, HTTP_X_TENANT_ID="   ")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("X-Tenant-ID", str(response.data))

    # ---- the header must not help ordinary users ----

    def test_header_cannot_widen_a_human_users_scope(self):
        response = self.human_client.get(
            AGENTS_URL, HTTP_X_TENANT_ID=str(self.tenant_b.id)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._ids(response), {str(self.agent_a.id)})

    def test_malformed_header_does_not_break_a_human_user(self):
        response = self.human_client.get(AGENTS_URL, HTTP_X_TENANT_ID="not-a-uuid")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # ---- writes: the agent field can no longer pick the tenant on its own ----

    def _create_payload(self, agent, provider_call_id):
        return {
            "provider_call_id": provider_call_id,
            "agent": str(agent.id),
            "caller_number": "+14155559999",
            "direction": AgentCallLog.CallDirection.OUTBOUND,
        }

    def test_create_rejects_agent_outside_header_tenant(self):
        response = self.runner.post(
            CALL_LOGS_URL,
            self._create_payload(self.agent_b, "cross-tenant-write"),
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant_a.id),
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            AgentCallLog.objects.filter(provider_call_id="cross-tenant-write").exists()
        )

    def test_create_allows_agent_inside_header_tenant(self):
        response = self.runner.post(
            CALL_LOGS_URL,
            self._create_payload(self.agent_a, "in-tenant-write"),
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant_a.id),
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        log = AgentCallLog.objects.get(provider_call_id="in-tenant-write")
        self.assertEqual(log.tenant_id, self.tenant_a.id)

    def test_created_log_takes_its_tenant_from_the_header(self):
        # Not from `agent`. The two must agree, but the header is what is written — so
        # the row is guaranteed to land where the caller said it was working, which is
        # where the humans who own it will look for it.
        response = self.runner.post(
            CALL_LOGS_URL,
            self._create_payload(self.agent_b, "header-write"),
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant_b.id),
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        log = AgentCallLog.objects.get(provider_call_id="header-write")
        self.assertEqual(log.tenant_id, self.tenant_b.id)

    def test_upload_url_rejects_agent_outside_header_tenant(self):
        response = self.runner.post(
            f"{CALL_LOGS_URL}request-upload-url/",
            {
                "agent": str(self.agent_b.id),
                "filename": "call.mp3",
                "content_type": "audio/mpeg",
                "file_size_in_bytes": 1024,
            },
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant_a.id),
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
