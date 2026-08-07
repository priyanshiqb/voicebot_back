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
        # Verify visual_icon, primary_accent, and is_active are included in the list output
        self.assertEqual(response.data["data"][0]["visual_icon"], "support")
        self.assertEqual(response.data["data"][0]["primary_accent"], "#123456")
        self.assertEqual(response.data["data"][0]["is_active"], True)

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

        # Correct payload with is_active = False on creation
        payload_ok = {
            "name": "Support Bot Template",
            "short_description": "A very detailed description of the template behavior.",
            "primary_accent": "#FFAA00",
            "agent_prompt": "You are a customer support agent. You must answer questions correctly.",
            "strict_facts": True,
            "can_user_edit_flow": True,
            "is_active": False,
        }
        response_ok = self.client_alex.post("/api/v1/core/agent-templates/", payload_ok, format="json")
        self.assertEqual(response_ok.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response_ok.data["data"]["name"], "Support Bot Template")
        self.assertEqual(response_ok.data["data"]["can_user_edit_flow"], True)
        self.assertEqual(response_ok.data["data"]["is_active"], False)

        template_id = response_ok.data["data"]["id"]
        # Update template to make is_active = True
        response_update = self.client_alex.patch(
            f"/api/v1/core/agent-templates/{template_id}/",
            {"is_active": True},
            format="json"
        )
        self.assertEqual(response_update.status_code, status.HTTP_200_OK)
        self.assertEqual(response_update.data["data"]["is_active"], True)

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

        # Validation check: duplicate question for tenant
        response_dup = self.client_alex.post("/api/v1/core/probing-questions/", payload, format="json")
        self.assertEqual(response_dup.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("question", response_dup.data["errors"])

        # A question created through the API is editable by the person who created it.
        pq_obj = ProbingQuestion.objects.get(id=pq_id)
        self.assertTrue(pq_obj.can_user_edit)

        response_edit = self.client_alex.patch(
            f"/api/v1/core/probing-questions/{pq_id}/",
            {"priority": 7},
            format="json",
        )
        self.assertEqual(response_edit.status_code, status.HTTP_200_OK)
        self.assertEqual(response_edit.data["data"]["priority"], 7)

        # Bulk delete
        response_del = self.client_alex.post("/api/v1/core/probing-questions/bulk_delete/", {"ids": [pq_id]}, format="json")
        self.assertEqual(response_del.status_code, status.HTTP_200_OK)

        # Verify soft deleted using all_objects manager
        pq = ProbingQuestion.all_objects.get(id=pq_id)
        self.assertTrue(pq.is_deleted)

    def test_probing_question_read_only_flow(self):
        # Create a read-only probing question via ORM directly
        pq = ProbingQuestion.objects.create(
            tenant=self.tenant,
            question="What is your social security number?",
            priority=3,
            score=90,
            is_mandatory=True,
            can_user_edit=False,
        )
        pq_id = pq.id

        # can_user_edit is not client-controlled: a caller asking for False still gets
        # an editable row, because the server forces True on anything created here.
        payload = {
            "question": "What is your passport number?",
            "priority": 3,
            "score": 90,
            "is_mandatory": True,
            "can_user_edit": False,
        }
        response = self.client_alex.post("/api/v1/core/probing-questions/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["data"]["can_user_edit"], True)

        # Attempt update read-only question -> 400 Bad Request
        response_update = self.client_alex.patch(
            f"/api/v1/core/probing-questions/{pq_id}/",
            {"priority": 5},
            format="json"
        )
        self.assertEqual(response_update.status_code, status.HTTP_400_BAD_REQUEST)

        # Attempt delete -> 200 OK (since is_default is False, deletion is allowed)
        response_delete = self.client_alex.delete(f"/api/v1/core/probing-questions/{pq_id}/")
        self.assertEqual(response_delete.status_code, status.HTTP_200_OK)

        # Re-create to test bulk delete
        pq2 = ProbingQuestion.objects.create(
            tenant=self.tenant,
            question="What is your credit score range?",
            priority=4,
            score=80,
            is_mandatory=False,
            can_user_edit=False,
        )
        # Attempt bulk delete -> 200 OK (since is_default is False, bulk deletion is allowed)
        response_bulk = self.client_alex.post(
            "/api/v1/core/probing-questions/bulk_delete/",
            {"ids": [pq2.id]},
            format="json"
        )
        self.assertEqual(response_bulk.status_code, status.HTTP_200_OK)

    def test_probing_question_is_default_flow(self):
        # Create a default probing question (tenant is None, is_default is True)
        default_pq = ProbingQuestion.objects.create(
            tenant=None,
            question="What is your full legal name?",
            priority=1,
            score=100,
            is_mandatory=True,
            is_default=True,
            can_user_edit=False,
        )

        # Retrieve/List from Alex's client -> should include the default question
        response_list = self.client_alex.get("/api/v1/core/probing-questions/")
        self.assertEqual(response_list.status_code, status.HTTP_200_OK)
        questions = [item["question"] for item in response_list.data["data"]]
        self.assertIn("What is your full legal name?", questions)

        # Attempt to update the default question -> 400 Bad Request
        response_update = self.client_alex.patch(
            f"/api/v1/core/probing-questions/{default_pq.id}/",
            {"priority": 2},
            format="json"
        )
        self.assertEqual(response_update.status_code, status.HTTP_400_BAD_REQUEST)

        # Attempt to delete the default question -> 400 Bad Request
        response_delete = self.client_alex.delete(f"/api/v1/core/probing-questions/{default_pq.id}/")
        self.assertEqual(response_delete.status_code, status.HTTP_400_BAD_REQUEST)

        # Attempt bulk delete on default question -> 400 Bad Request
        response_bulk = self.client_alex.post(
            "/api/v1/core/probing-questions/bulk_delete/",
            {"ids": [str(default_pq.id)]},
            format="json"
        )
        self.assertEqual(response_bulk.status_code, status.HTTP_400_BAD_REQUEST)

    def test_agent_view_flow(self):
        # Create KnowledgeBase
        kb = KnowledgeBase.objects.create(tenant=self.tenant, name="Standard KB")

        # Create Agent
        payload = {
            "bot_name": "Acme-Voicebot",
            "persona_name": "Oliver-Persona",
            "languages": ["en-IN", "en-US"],
            "tone": "PROFESSIONAL",
            "agent_template": str(self.template.id),
            "voice_name": "en-IN-Wavenet-B",
            "add_knowledge_bases": [str(kb.id)],
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

    def test_agent_template_is_default_flow(self):
        # Create a default agent template (tenant is None, is_default is True)
        default_tpl = AgentTemplate.objects.create(
            tenant=None,
            name="Global Standard Template",
            short_description="A globally shared system template.",
            visual_icon="global",
            primary_accent="#000000",
            agent_prompt="You are a standard bot answering queries globally.",
            is_default=True,
        )

        # Retrieve/List from Alex's client -> should include the default template
        response_list = self.client_alex.get("/api/v1/core/agent-templates/")
        self.assertEqual(response_list.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response_list.data["data"]]
        self.assertIn("Global Standard Template", names)

        # Attempt to update the default template -> 400 Bad Request
        response_update = self.client_alex.patch(
            f"/api/v1/core/agent-templates/{default_tpl.id}/",
            {"short_description": "New modified template description."},
            format="json"
        )
        self.assertEqual(response_update.status_code, status.HTTP_400_BAD_REQUEST)

        # Attempt to delete the default template -> 400 Bad Request
        response_delete = self.client_alex.delete(f"/api/v1/core/agent-templates/{default_tpl.id}/")
        self.assertEqual(response_delete.status_code, status.HTTP_400_BAD_REQUEST)

        # Attempt bulk delete on default template -> 400 Bad Request
        response_bulk = self.client_alex.post(
            "/api/v1/core/agent-templates/bulk_delete/",
            {"ids": [str(default_tpl.id)]},
            format="json"
        )
        self.assertEqual(response_bulk.status_code, status.HTTP_400_BAD_REQUEST)

    def test_agent_template_probing_questions_m2m_flow(self):
        # 1. Create probing questions
        pq_alex = ProbingQuestion.objects.create(
            tenant=self.tenant,
            question="What is your first name?",
            priority=1,
            score=10,
        )
        pq_bob = ProbingQuestion.objects.create(
            tenant=self.other_tenant,
            question="What is your pet name?",
            priority=1,
            score=10,
        )
        pq_default = ProbingQuestion.objects.create(
            tenant=None,
            question="What is your age range?",
            priority=2,
            score=20,
            is_default=True,
        )

        # 2. Alex creates an agent template with pq_alex and pq_default (Valid)
        payload = {
            "name": "Custom M2M Template",
            "short_description": "A template testing Many-to-Many associations.",
            "agent_prompt": "You are a customer assistant testing Many-to-Many links.",
            "add_probing_questions": [str(pq_alex.id), str(pq_default.id)]
        }
        response = self.client_alex.post("/api/v1/core/agent-templates/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        pqs_in_response = [item["id"] for item in response.data["data"]["probing_questions"]]
        self.assertIn(str(pq_alex.id), pqs_in_response)
        self.assertIn(str(pq_default.id), pqs_in_response)

        template_id = response.data["data"]["id"]

        # 3. Alex attempts to update/add Bob's probing question (Invalid - tenant mismatch)
        response_err = self.client_alex.patch(
            f"/api/v1/core/agent-templates/{template_id}/",
            {"add_probing_questions": [str(pq_bob.id)]},
            format="json"
        )
        self.assertEqual(response_err.status_code, status.HTTP_400_BAD_REQUEST)

        # 4. Alex successfully updates/removes a probing question
        response_ok = self.client_alex.patch(
            f"/api/v1/core/agent-templates/{template_id}/",
            {"remove_probing_questions": [str(pq_alex.id)]},
            format="json"
        )
        self.assertEqual(response_ok.status_code, status.HTTP_200_OK)
        pqs_after = [item["id"] for item in response_ok.data["data"]["probing_questions"]]
        self.assertNotIn(str(pq_alex.id), pqs_after)
        self.assertIn(str(pq_default.id), pqs_after)

    def test_agent_template_incremental_probing_questions_flow(self):
        # 1. Create probing questions
        pq_alex = ProbingQuestion.objects.create(
            tenant=self.tenant,
            question="What is your first name?",
            priority=1,
            score=10,
        )
        pq_bob = ProbingQuestion.objects.create(
            tenant=self.other_tenant,
            question="What is your pet name?",
            priority=1,
            score=10,
        )
        pq_default = ProbingQuestion.objects.create(
            tenant=None,
            question="What is your age range?",
            priority=2,
            score=20,
            is_default=True,
        )

        # Create Agent Template associated with pq_alex
        tpl = AgentTemplate.objects.create(
            tenant=self.tenant,
            name="Incremental M2M Template",
            short_description="A template testing Incremental Many-to-Many associations.",
            agent_prompt="You are a customer assistant testing Many-to-Many links.",
        )
        tpl.probing_questions.add(pq_alex)

        # 2. Patch with add action for pq_default (Valid)
        response = self.client_alex.patch(
            f"/api/v1/core/agent-templates/{tpl.id}/",
            {"add_probing_questions": [str(pq_default.id)]},
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pqs = [item["id"] for item in response.data["data"]["probing_questions"]]
        self.assertIn(str(pq_alex.id), pqs)
        self.assertIn(str(pq_default.id), pqs)

        # 3. Patch with remove action for pq_alex (Valid)
        response_rm = self.client_alex.patch(
            f"/api/v1/core/agent-templates/{tpl.id}/",
            {"remove_probing_questions": [str(pq_alex.id)]},
            format="json"
        )
        self.assertEqual(response_rm.status_code, status.HTTP_200_OK)
        pqs_rm = [item["id"] for item in response_rm.data["data"]["probing_questions"]]
        self.assertNotIn(str(pq_alex.id), pqs_rm)
        self.assertIn(str(pq_default.id), pqs_rm)

        # 4. Patch adding Bob's question (Invalid - tenant mismatch)
        response_err = self.client_alex.patch(
            f"/api/v1/core/agent-templates/{tpl.id}/",
            {"add_probing_questions": [str(pq_bob.id)]},
            format="json"
        )
        self.assertEqual(response_err.status_code, status.HTTP_400_BAD_REQUEST)

        # 5. Patch default template (Invalid)
        default_tpl = AgentTemplate.objects.create(
            tenant=None,
            name="Global Standard Template",
            short_description="A globally shared system template.",
            agent_prompt="You are a standard bot answering queries globally.",
            is_default=True,
        )
        response_default_err = self.client_alex.patch(
            f"/api/v1/core/agent-templates/{default_tpl.id}/",
            {"add_probing_questions": [str(pq_default.id)]},
            format="json"
        )
        self.assertEqual(response_default_err.status_code, status.HTTP_400_BAD_REQUEST)

    def test_agent_incremental_knowledge_bases_flow(self):
        # 1. Create Knowledge Bases
        kb_alex = KnowledgeBase.objects.create(tenant=self.tenant, name="Alex KB")
        kb_bob = KnowledgeBase.objects.create(tenant=self.other_tenant, name="Bob KB")
        kb_extra = KnowledgeBase.objects.create(tenant=self.tenant, name="Alex Extra KB")

        # 2. Create Agent
        agent = Agent.objects.create(
            tenant=self.tenant,
            bot_name="Agent-Inc",
            persona_name="Alice",
            tone="PROFESSIONAL",
            agent_template=self.template,
        )
        agent.knowledge_bases.add(kb_alex)

        # 3. Patch with add action for kb_extra (Valid)
        response = self.client_alex.patch(
            f"/api/v1/core/agents/{agent.id}/",
            {"add_knowledge_bases": [str(kb_extra.id)]},
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        kbs = [item["id"] for item in response.data["data"]["knowledge_bases"]]
        self.assertIn(str(kb_alex.id), kbs)
        self.assertIn(str(kb_extra.id), kbs)

        # 4. Patch with remove action for kb_alex (Valid)
        response_rm = self.client_alex.patch(
            f"/api/v1/core/agents/{agent.id}/",
            {"remove_knowledge_bases": [str(kb_alex.id)]},
            format="json"
        )
        self.assertEqual(response_rm.status_code, status.HTTP_200_OK)
        kbs_rm = [item["id"] for item in response_rm.data["data"]["knowledge_bases"]]
        self.assertNotIn(str(kb_alex.id), kbs_rm)
        self.assertIn(str(kb_extra.id), kbs_rm)

        # 5. Patch adding Bob's KB (Invalid - tenant mismatch)
        response_err = self.client_alex.patch(
            f"/api/v1/core/agents/{agent.id}/",
            {"add_knowledge_bases": [str(kb_bob.id)]},
            format="json"
        )
        self.assertEqual(response_err.status_code, status.HTTP_400_BAD_REQUEST)

        # 6. Patch with both knowledge_bases and add_knowledge_bases (Invalid - mutually exclusive)
        response_excl_err = self.client_alex.patch(
            f"/api/v1/core/agents/{agent.id}/",
            {
                "knowledge_bases": [{"id": str(kb_alex.id)}],
                "add_knowledge_bases": [str(kb_extra.id)],
            },
            format="json"
        )
        self.assertEqual(response_excl_err.status_code, status.HTTP_400_BAD_REQUEST)

    def test_agent_incremental_probing_questions_flow(self):
        # 1. Create probing questions
        pq_alex = ProbingQuestion.objects.create(
            tenant=self.tenant,
            question="What is your first name?",
            priority=1,
            score=10,
        )
        pq_bob = ProbingQuestion.objects.create(
            tenant=self.other_tenant,
            question="What is your pet name?",
            priority=1,
            score=10,
        )
        pq_default = ProbingQuestion.objects.create(
            tenant=None,
            question="What is your age range?",
            priority=2,
            score=20,
            is_default=True,
        )

        # 2. Create Agent
        agent = Agent.objects.create(
            tenant=self.tenant,
            bot_name="Agent-PQ-Inc",
            persona_name="Alice",
            tone="PROFESSIONAL",
            agent_template=self.template,
        )
        agent.probing_questions.add(pq_alex)

        # 3. Patch with add action for pq_default (Valid)
        response = self.client_alex.patch(
            f"/api/v1/core/agents/{agent.id}/",
            {"add_probing_questions": [str(pq_default.id)]},
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pqs = [item["id"] for item in response.data["data"]["probing_questions"]]
        self.assertIn(str(pq_alex.id), pqs)
        self.assertIn(str(pq_default.id), pqs)

        # 4. Patch with remove action for pq_alex (Valid)
        response_rm = self.client_alex.patch(
            f"/api/v1/core/agents/{agent.id}/",
            {"remove_probing_questions": [str(pq_alex.id)]},
            format="json"
        )
        self.assertEqual(response_rm.status_code, status.HTTP_200_OK)
        pqs_rm = [item["id"] for item in response_rm.data["data"]["probing_questions"]]
        self.assertNotIn(str(pq_alex.id), pqs_rm)
        self.assertIn(str(pq_default.id), pqs_rm)

        # 5. Patch adding Bob's PQ (Invalid - tenant mismatch)
        response_err = self.client_alex.patch(
            f"/api/v1/core/agents/{agent.id}/",
            {"add_probing_questions": [str(pq_bob.id)]},
            format="json"
        )
        self.assertEqual(response_err.status_code, status.HTTP_400_BAD_REQUEST)

        # 6. Patch with both probing_questions and add_probing_questions (Invalid - read-only validation check)
        response_excl_err = self.client_alex.patch(
            f"/api/v1/core/agents/{agent.id}/",
            {
                "probing_questions": [str(pq_alex.id)],
                "add_probing_questions": [str(pq_default.id)],
            },
            format="json"
        )
        self.assertEqual(response_excl_err.status_code, status.HTTP_400_BAD_REQUEST)

    def test_agent_template_delete_linked_validation(self):
        # 1. Create an agent using the template
        agent = Agent.objects.create(
            tenant=self.tenant,
            bot_name="Agent-Link-Tpl",
            persona_name="Alice",
            tone="PROFESSIONAL",
            agent_template=self.template,
        )
        # Attempt to delete the template -> 400 Bad Request
        response = self.client_alex.delete(f"/api/v1/core/agent-templates/{self.template.id}/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("selected in the following agents", response.data["errors"]["detail"])
        
        # Attempt bulk delete -> 400 Bad Request
        response_bulk = self.client_alex.post(
            "/api/v1/core/agent-templates/bulk_delete/",
            {"ids": [str(self.template.id)]},
            format="json"
        )
        self.assertEqual(response_bulk.status_code, status.HTTP_400_BAD_REQUEST)
        
        # 2. Delete the agent, then we should be able to delete the template
        agent.is_deleted = True
        agent.save()
        
        response_ok = self.client_alex.delete(f"/api/v1/core/agent-templates/{self.template.id}/")
        self.assertEqual(response_ok.status_code, status.HTTP_200_OK)

    def test_probing_question_delete_linked_validation(self):
        # 1. Create a probing question
        pq = ProbingQuestion.objects.create(
            tenant=self.tenant,
            question="What is your linked question?",
            priority=1,
            score=10,
        )
        # Link to agent
        agent = Agent.objects.create(
            tenant=self.tenant,
            bot_name="Agent-Link-PQ",
            persona_name="Alice",
            tone="PROFESSIONAL",
            agent_template=self.template,
        )
        agent.probing_questions.add(pq)
        
        # Attempt to delete -> 400 Bad Request
        response = self.client_alex.delete(f"/api/v1/core/probing-questions/{pq.id}/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        
        # Attempt bulk delete -> 400 Bad Request
        response_bulk = self.client_alex.post(
            "/api/v1/core/probing-questions/bulk_delete/",
            {"ids": [str(pq.id)]},
            format="json"
        )
        self.assertEqual(response_bulk.status_code, status.HTTP_400_BAD_REQUEST)
        
        # Remove from agent, but add to template
        agent.is_deleted = True
        agent.save()
        
        self.template.probing_questions.add(pq)
        
        # Attempt to delete again -> 400 Bad Request (linked to template)
        response_tpl = self.client_alex.delete(f"/api/v1/core/probing-questions/{pq.id}/")
        self.assertEqual(response_tpl.status_code, status.HTTP_400_BAD_REQUEST)


class ProbingQuestionExportColumnsTestCase(APITestCase):
    """
    The export carries exactly five columns, and nothing else.

    Guarded rather than assumed because the obvious way to change this list —
    editing `Meta.fields` on the resource — silently does not work for `id`. It is
    a declared field on `TenantModelResource`, and import-export appends every
    declared field to the export order after the ones `Meta.fields` names, so
    removing it there moves the column to the end of the file instead of dropping
    it. Without this test that regression looks like a tidy-up and reads as a no-op.
    """

    EXPECTED = ["question", "priority", "score", "is_mandatory", "is_active"]

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme Corp", subdomain="acme-export")
        self.user = User.objects.create_user(
            username="export-alex", email="export-alex@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)

        ProbingQuestion.objects.create(
            tenant=self.tenant,
            question="What is your approved budget?",
            priority=1,
            score=40,
            is_mandatory=True,
        )

    def test_the_exported_csv_has_exactly_the_agreed_columns(self):
        response = self.client_alex.get("/api/v1/core/probing-questions/export/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        body = response.content.decode("utf-8-sig").splitlines()
        self.assertEqual(body[0].strip().split(","), self.EXPECTED)
        # The row lines up with the header — a column dropped from one and not the
        # other would still produce a plausible-looking file.
        # Booleans render as 1/0, which is what import-export writes and reads back.
        self.assertEqual(
            body[1].strip().split(","),
            ["What is your approved budget?", "1", "40", "1", "1"],
        )

    def test_id_stays_importable_even_though_it_is_not_exported(self):
        """`import_id_fields` defaults to ("id",) and is resolved through
        `self.fields`; drop it there and every import raises KeyError rather than
        falling back to creating rows."""
        from core.resources.probing_question import ProbingQuestionResource

        resource = ProbingQuestionResource()
        self.assertIn("id", resource.fields)
        self.assertEqual(resource.get_import_id_fields(), ["id"])
        self.assertNotIn("id", resource.get_export_headers())


class AgentTemplateExportColumnsTestCase(APITestCase):
    """
    Same contract as the probing export, and guarded for the same reason: `id`
    cannot be dropped through `Meta.fields`, so a future "tidy-up" there would put
    the column back without looking like a change in behaviour. See
    `TenantModelResource.EXPORT_FIELDS`.
    """

    EXPECTED = [
        "name",
        "short_description",
        "industry",
        "strict_facts",
        "can_user_edit_flow",
        "is_active",
    ]

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Acme Corp", subdomain="acme-tpl-export"
        )
        self.user = User.objects.create_user(
            username="tpl-alex", email="tpl-alex@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)

        AgentTemplate.objects.create(
            tenant=self.tenant,
            name="Acme Template",
            short_description="Handles inbound support, billing, and returns.",
            industry=AgentTemplate.IndustryChoices.E_COMMERCE,
            agent_prompt="You are a helpful customer assistant assisting customers.",
            strict_facts=True,
            can_user_edit_flow=False,
        )

    def _rows(self):
        import csv
        import io

        response = self.client_alex.get("/api/v1/core/agent-templates/export/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))

    def test_the_exported_csv_has_exactly_the_agreed_columns(self):
        rows = self._rows()
        self.assertEqual(rows[0], self.EXPECTED)
        # Parsed with csv rather than split(","): short_description is free text and
        # a comma in it would otherwise look like an extra column.
        self.assertEqual(
            rows[1],
            [
                "Acme Template",
                "Handles inbound support, billing, and returns.",
                "E_COMMERCE",
                "1",
                "0",
                "1",
            ],
        )

    def test_id_stays_importable_even_though_it_is_not_exported(self):
        from core.resources.agent_template import AgentTemplateResource

        resource = AgentTemplateResource()
        self.assertIn("id", resource.fields)
        self.assertEqual(resource.get_import_id_fields(), ["id"])
        self.assertNotIn("id", resource.get_export_headers())
