from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User
from core.models.agent_template import AgentTemplate
from core.models.probing_question import ProbingQuestion
from core.models.knowledge_base import KnowledgeBase
from core.models.agent import Agent


class AgentProbingViewsTestCase(APITestCase):
    def setUp(self):
        # Create Tenants
        self.tenant = Tenant.objects.create(name="Acme Corp", subdomain="acme")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex")

        # Create Users
        self.user = User.objects.create_user(username="alex", email="alex@example.com", password="password")
        self.other_user = User.objects.create_user(username="bob", email="bob@example.com", password="password")

        # Establish Tenant Mappings
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.other_user, tenant=self.other_tenant)

        # Base templates for testing
        self.template = AgentTemplate.objects.create(
            tenant=self.tenant,
            name="Acme Template",
            short_description="A template for Acme bot.",
            visual_icon="support",
            primary_accent="#123456",
            agent_prompt="You are a helpful customer assistant assisting customers.",
            strict_facts=True,
            can_user_edit_flow=True,
        )
        self.other_template = AgentTemplate.objects.create(
            tenant=self.other_tenant,
            name="Globex Template",
            short_description="A template for Globex bot.",
            agent_prompt="You are a helpful customer assistant assisting customers.",
        )

        # Clients configured with authentication
        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)

        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.other_user)

    def test_agent_template_list_isolation(self):
        # Alex (Acme Corp) should see 1 Agent Template
        response = self.client_alex.get("/api/v1/core/agent-templates/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["name"], "Acme Template")

        # Bob (Globex) should see 1 Agent Template (Globex's)
        response_other = self.client_bob.get("/api/v1/core/agent-templates/")
        self.assertEqual(response_other.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response_other.data["data"]), 1)
        self.assertEqual(response_other.data["data"][0]["name"], "Globex Template")

    def test_agent_template_retrieve_isolation(self):
        # Alex should retrieve their own template
        response = self.client_alex.get(f"/api/v1/core/agent-templates/{self.template.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["name"], "Acme Template")

        # Alex should not retrieve Bob's template (returns 404 due to tenant filtering)
        response_other = self.client_alex.get(f"/api/v1/core/agent-templates/{self.other_template.id}/")
        self.assertEqual(response_other.status_code, status.HTTP_404_NOT_FOUND)

    def test_agent_template_create_validation(self):
        # Validation checks
        payload = {
            "name": "Invalid Template @#",
            "short_description": "Short",
            "primary_accent": "123456",
            "agent_prompt": "Too short prompt",
            "strict_facts": True,
            "can_user_edit_flow": False
        }
        response = self.client_alex.post("/api/v1/core/agent-templates/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data["errors"])
        self.assertIn("short_description", response.data["errors"])
        self.assertIn("primary_accent", response.data["errors"])
        self.assertIn("agent_prompt", response.data["errors"])

        # Correct payload
        payload_ok = {
            "name": "Support Bot Template",
            "short_description": "A very detailed description of the template behavior.",
            "primary_accent": "#FFAA00",
            "agent_prompt": "You are a customer support agent. You must answer questions correctly.",
            "strict_facts": True,
            "can_user_edit_flow": True
        }
        response_ok = self.client_alex.post("/api/v1/core/agent-templates/", payload_ok, format="json")
        self.assertEqual(response_ok.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response_ok.data["data"]["name"], "Support Bot Template")
        self.assertEqual(response_ok.data["data"]["can_user_edit_flow"], True)

    def test_agent_template_uniqueness_validation(self):
        # Trying to create template with duplicate name on same tenant
        payload = {
            "name": "Acme Template",
            "short_description": "A very detailed description of the template behavior.",
            "primary_accent": "#FFAA00",
            "agent_prompt": "You are a customer support agent. You must answer questions correctly.",
        }
        response = self.client_alex.post("/api/v1/core/agent-templates/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data["errors"])

    def test_probing_question_view_flow(self):
        # Create probing question
        payload = {
            "agent_template": str(self.template.id),
            "question": "What is your account number?",
            "priority": 2,
            "score": 50,
            "is_mandatory": True,
        }
        response = self.client_alex.post("/api/v1/core/probing-questions/", payload, format="json")
        if response.status_code != status.HTTP_201_CREATED:
            print("probing_question response data:", response.data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["question"], "What is your account number?")

        pq_id = response.data["data"]["id"]

        # List probing questions
        response_list = self.client_alex.get("/api/v1/core/probing-questions/")
        self.assertEqual(response_list.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response_list.data["data"]), 1)

        # Validation check: duplicate question for template
        response_dup = self.client_alex.post("/api/v1/core/probing-questions/", payload, format="json")
        self.assertEqual(response_dup.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("question", response_dup.data["errors"])

        # Bulk delete
        response_del = self.client_alex.post("/api/v1/core/probing-questions/bulk_delete/", {"ids": [pq_id]}, format="json")
        self.assertEqual(response_del.status_code, status.HTTP_200_OK)

        # Verify soft deleted using all_objects manager
        pq = ProbingQuestion.all_objects.get(id=pq_id)
        self.assertTrue(pq.is_deleted)

    def test_agent_view_flow(self):
        # Create KnowledgeBase
        kb = KnowledgeBase.objects.create(tenant=self.tenant, name="Standard KB")

        # Create Agent
        payload = {
            "bot_name": "Acme-Voicebot",
            "persona_name": "Oliver-Persona",
            "languages": ["en-IN", "en-US"],
            "tone": "PROFESSIONAL",
            "industry": "GENERAL_ASSISTANT",
            "agent_template": str(self.template.id),
            "voice_name": "en-IN-Wavenet-B",
            "knowledge_bases": [str(kb.id)],
            "transfer_destinations": {"support": "+1234567890"}
        }
        response = self.client_alex.post("/api/v1/core/agents/", payload, format="json")
        if response.status_code != status.HTTP_201_CREATED:
            print("agent response data:", response.data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["bot_name"], "Acme-Voicebot")
        self.assertEqual(len(response.data["data"]["knowledge_bases"]), 1)

        agent_id = response.data["data"]["id"]

        # Verify tenant isolation on retrieve
        response_bob = self.client_bob.get(f"/api/v1/core/agents/{agent_id}/")
        self.assertEqual(response_bob.status_code, status.HTTP_404_NOT_FOUND)

        # Verify bulk delete
        response_del = self.client_alex.post("/api/v1/core/agents/bulk_delete/", {"ids": [agent_id]}, format="json")
        self.assertEqual(response_del.status_code, status.HTTP_200_OK)

        # Verify soft deleted bot name timestamp append using all_objects manager
        agent = Agent.all_objects.get(id=agent_id)
        self.assertTrue(agent.is_deleted)
        self.assertIn("-deleted-", agent.bot_name)


