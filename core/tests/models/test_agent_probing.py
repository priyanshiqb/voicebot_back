from django.test import TestCase
from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError
from tenant.models.Tenant import Tenant
from core.models.agent_template import AgentTemplate
from core.models.probing_question import ProbingQuestion
from core.models.knowledge_base import KnowledgeBase
from core.models.agent import Agent


class AgentProbingTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Alpha Corp",
            subdomain="alpha-test"
        )
        self.other_tenant = Tenant.objects.create(
            name="Beta Corp",
            subdomain="beta-test"
        )

    def test_agent_template_creation(self):
        template = AgentTemplate.objects.create(
            tenant=self.tenant,
            name="Support Advisor",
            short_description="A template for customer support queries.",
            visual_icon="support-icon",
            primary_accent="#00FF00",
            agent_prompt="You are a support agent assisting customers. Ask context questions.",
            strict_facts=True,
        )

        self.assertEqual(template.name, "Support Advisor")
        self.assertEqual(template.tenant, self.tenant)
        self.assertTrue(template.strict_facts)


    def test_agent_template_validation(self):
        # Invalid Name (less than 2 chars)
        t = AgentTemplate(
            tenant=self.tenant,
            name="A",
            short_description="Short desc of bot template.",
            agent_prompt="A prompt description that has more than thirty characters.",
        )
        with self.assertRaises(ValidationError):
            t.full_clean(exclude=["created_by", "updated_by", "deleted_by"])

        # Invalid Name (illegal character like '@')
        t2 = AgentTemplate(
            tenant=self.tenant,
            name="Support @ Advisor",
            short_description="Short desc of bot template.",
            agent_prompt="A prompt description that has more than thirty characters.",
        )
        with self.assertRaises(ValidationError):
            t2.full_clean(exclude=["created_by", "updated_by", "deleted_by"])

        # Valid Name (contains letters, spaces, hyphens, underscores)
        t3 = AgentTemplate(
            tenant=self.tenant,
            name="Support_Advisor-01",
            short_description="Short desc of bot template.",
            agent_prompt="A prompt description that has more than thirty characters.",
        )
        t3.full_clean(exclude=["created_by", "updated_by", "deleted_by"])  # should not raise

    def test_agent_template_uniqueness(self):
        AgentTemplate.objects.create(
            tenant=self.tenant,
            name="Standard Bot",
            short_description="Short desc of bot template.",
            agent_prompt="A prompt description that has more than thirty characters.",
        )

        # Duplicate template name on same tenant -> IntegrityError
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AgentTemplate.objects.create(
                    tenant=self.tenant,
                    name="Standard Bot",
                    short_description="Other desc of bot template.",
                    agent_prompt="A prompt description that has more than thirty characters.",
                )

        # Same template name on different tenant -> Allowed
        other_t = AgentTemplate.objects.create(
            tenant=self.other_tenant,
            name="Standard Bot",
            short_description="Other desc of bot template.",
            agent_prompt="A prompt description that has more than thirty characters.",
        )
        self.assertEqual(other_t.tenant, self.other_tenant)

    def test_agent_template_soft_delete(self):
        t = AgentTemplate.objects.create(
            tenant=self.tenant,
            name="Standard Bot",
            short_description="Short desc of bot template.",
            agent_prompt="A prompt description that has more than thirty characters.",
        )

        t.soft_delete()
        t.refresh_from_db()

        self.assertTrue(t.is_deleted)
        self.assertIn("-deleted-", t.name)

        # Re-creating a template with the same name after deletion is allowed
        new_t = AgentTemplate.objects.create(
            tenant=self.tenant,
            name="Standard Bot",
            short_description="Short desc of bot template.",
            agent_prompt="A prompt description that has more than thirty characters.",
        )
        self.assertEqual(new_t.name, "Standard Bot")

    def test_agent_template_default_soft_delete(self):
        t = AgentTemplate.objects.create(
            tenant=None,
            name="Default standard bot",
            short_description="Default system-wide agent template.",
            agent_prompt="A prompt description that has more than thirty characters.",
            is_default=True,
        )
        with self.assertRaises(ValidationError):
            t.soft_delete()

    def test_probing_question_creation(self):
        q = ProbingQuestion.objects.create(
            tenant=self.tenant,
            question="Is your monthly salary above $5000?",
            priority=2,
            score=80,
            is_mandatory=True,
        )

        self.assertEqual(q.priority, 2)
        self.assertEqual(q.score, 80)
        self.assertTrue(q.is_mandatory)

    def test_probing_question_validation(self):
        # Question too short (less than 10 chars)
        q1 = ProbingQuestion(
            tenant=self.tenant,
            question="Short?",
            priority=1,
            score=50,
        )
        with self.assertRaises(ValidationError):
            q1.full_clean(exclude=["created_by", "updated_by", "deleted_by"])

        # Priority too small (less than 1)
        q2 = ProbingQuestion(
            tenant=self.tenant,
            question="Is your monthly salary above $5000?",
            priority=0,
            score=50,
        )
        with self.assertRaises(ValidationError):
            q2.full_clean(exclude=["created_by", "updated_by", "deleted_by"])

        # Score out of range (more than 100)
        q3 = ProbingQuestion(
            tenant=self.tenant,
            question="Is your monthly salary above $5000?",
            priority=1,
            score=101,
        )
        with self.assertRaises(ValidationError):
            q3.full_clean(exclude=["created_by", "updated_by", "deleted_by"])


    def test_probing_question_uniqueness(self):
        ProbingQuestion.objects.create(
            tenant=self.tenant,
            question="Are you looking for short term investments?",
            priority=1,
            score=50,
        )

        # Duplicate question text on same tenant -> IntegrityError
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProbingQuestion.objects.create(
                    tenant=self.tenant,
                    question="Are you looking for short term investments?",
                    priority=2,
                    score=60,
                )

        # Same question text on different tenant -> Allowed
        other_q = ProbingQuestion.objects.create(
            tenant=self.other_tenant,
            question="Are you looking for short term investments?",
            priority=1,
            score=50,
        )
        self.assertEqual(other_q.tenant, self.other_tenant)

    def test_probing_question_default_soft_delete(self):
        q = ProbingQuestion.objects.create(
            tenant=None,
            question="This default question cannot be deleted.",
            priority=1,
            score=50,
            is_default=True,
            can_user_edit=False,
        )
        with self.assertRaises(ValidationError):
            q.soft_delete()

    def test_agent_creation(self):
        t = AgentTemplate.objects.create(
            tenant=self.tenant,
            name="Finance Bot Template",
            short_description="Short desc of template.",
            agent_prompt="A prompt description that has more than thirty characters.",
            industry=AgentTemplate.IndustryChoices.CLINIC_RECEPTIONIST,
        )
        kb = KnowledgeBase.objects.create(
            tenant=self.tenant,
            name="Customer Support KB"
        )
        agent = Agent.objects.create(
            tenant=self.tenant,
            bot_name="Quantum-Bot",
            persona_name="Emma",
            languages=["en-IN", "hi-IN"],
            tone=Agent.ToneChoices.FRIENDLY,
            agent_template=t,
            voice_name="Gemma",
            transfer_destinations={"destinations": []}
        )
        agent.knowledge_bases.add(kb)

        self.assertEqual(agent.bot_name, "Quantum-Bot")
        self.assertEqual(agent.persona_name, "Emma")
        self.assertEqual(agent.agent_template, t)
        self.assertEqual(agent.knowledge_bases.count(), 1)
        self.assertEqual(agent.tone, Agent.ToneChoices.FRIENDLY)

    def test_agent_validation(self):
        # Bot Name too short
        a1 = Agent(
            tenant=self.tenant,
            bot_name="Q",
            persona_name="Emma",
            agent_template=None,
        )
        with self.assertRaises(ValidationError):
            a1.full_clean(exclude=["created_by", "updated_by", "deleted_by"])

        # Persona Name has invalid characters (e.g. '$')
        a2 = Agent(
            tenant=self.tenant,
            bot_name="Quantum-Bot",
            persona_name="Emma$",
            agent_template=None,
        )
        with self.assertRaises(ValidationError):
            a2.full_clean(exclude=["created_by", "updated_by", "deleted_by"])

    def test_agent_uniqueness(self):
        Agent.objects.create(
            tenant=self.tenant,
            bot_name="Quantum-Bot",
            persona_name="Emma",
        )

        # Duplicate bot_name on same tenant -> IntegrityError
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Agent.objects.create(
                    tenant=self.tenant,
                    bot_name="Quantum-Bot",
                    persona_name="Archie",
                )

        # Same bot_name on different tenant -> Allowed
        other_agent = Agent.objects.create(
            tenant=self.other_tenant,
            bot_name="Quantum-Bot",
            persona_name="Emma",
        )
        self.assertEqual(other_agent.tenant, self.other_tenant)

    def test_agent_soft_delete(self):
        agent = Agent.objects.create(
            tenant=self.tenant,
            bot_name="Quantum-Bot",
            persona_name="Emma",
        )

        agent.soft_delete()
        agent.refresh_from_db()

        self.assertTrue(agent.is_deleted)
        self.assertIn("-deleted-", agent.bot_name)

        # Creating new agent with same bot_name after soft deletion -> Allowed
        new_agent = Agent.objects.create(
            tenant=self.tenant,
            bot_name="Quantum-Bot",
            persona_name="Emma",
        )
        self.assertEqual(new_agent.bot_name, "Quantum-Bot")

    def test_soft_delete_in_memory_state_synchronization(self):
        """Verify that soft_delete mutates the Python instance attributes directly in memory"""
        # Test AgentTemplate
        template = AgentTemplate.objects.create(
            tenant=self.tenant,
            name="In Memory Template",
            short_description="Short desc",
            agent_prompt="A prompt description that has more than thirty characters.",
        )
        template.soft_delete()
        self.assertTrue(template.is_deleted)
        self.assertIsNotNone(template.deleted_at)
        self.assertIn("-deleted-", template.name)

        # Test ProbingQuestion
        pq = ProbingQuestion.objects.create(
            tenant=self.tenant,
            question="Is your monthly salary above $5000?",
            priority=1,
            score=50,
        )
        pq.soft_delete()
        self.assertTrue(pq.is_deleted)
        self.assertIsNotNone(pq.deleted_at)
        self.assertIn("-deleted-", pq.question)

        # Test Agent
        agent = Agent.objects.create(
            tenant=self.tenant,
            bot_name="In-Memory-Agent",
            persona_name="Emma",
        )
        agent.soft_delete()
        self.assertTrue(agent.is_deleted)
        self.assertIsNotNone(agent.deleted_at)
        self.assertIn("-deleted-", agent.bot_name)

