from django.test import TestCase, RequestFactory
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from rest_framework.exceptions import ValidationError
from unittest.mock import patch


from tenant.models.Tenant import Tenant
from core.models.knowledge_base import KBText, KBFiles, KBQuestions, KBQuestionAnswer, KBWebsite, KnowledgeBase
from core.serializers import (
    KBTextSerializer,
    KBTextListSerializer,
    KBTextDropdownSerializer,
    KBFilesSerializer,
    KBWebsiteSerializer,
    KnowledgeBaseSerializer,
    KnowledgeBaseETLSerializer
)

User = get_user_model()

class KBSerializersTestCase(TestCase):
    def setUp(self):
        # Setup tenant mappings
        self.tenant = Tenant.objects.create(name="Acme Tech", subdomain="acme")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex")

        # Mock User mappings mimicking active tenant structures
        self.user = User.objects.create_user(username="alex", email="alex@example.com", password="password")
        self.other_user = User.objects.create_user(username="bob", email="bob@example.com", password="password")

        from tenant.models.TenantUserMapping import TenantUserMapping
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.other_user, tenant=self.other_tenant)

        # Mock requests
        self.factory = RequestFactory()
        self.request = self.factory.post('/api/kb/')
        self.request.user = self.user

        self.other_request = self.factory.post('/api/kb/')
        self.other_request.user = self.other_user

    def test_kb_text_serializer_read_only_fields(self):
        # 1. Main Serializer Write
        serializer = KBTextSerializer(
            data={"title": "Main Title", "description": "Guides", "is_processed": True},
            context={"request": self.request}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        instance = serializer.save(tenant=self.tenant)

        # Assert read only blocks
        self.assertFalse(instance.is_processed)  # The True was safely blocked as read-only!
        self.assertEqual(instance.title, "Main Title")

        # 2. List Serializer
        list_serializer = KBTextListSerializer(instance)
        self.assertEqual(list_serializer.data["title"], "Main Title")

        # 3. Dropdown Serializer
        dropdown_serializer = KBTextDropdownSerializer(instance)
        self.assertEqual(dropdown_serializer.data["label"], "Main Title")
        self.assertEqual(dropdown_serializer.data["value"], str(instance.id))

    def test_kb_files_serializer_validations(self):
        # 1. Invalid Format Check
        bad_file = SimpleUploadedFile("malicious.exe", b"exe data", content_type="application/octet-stream")
        serializer = KBFilesSerializer(
            data={"file": bad_file},
            context={"request": self.request}
        )
        with self.assertRaises(ValidationError) as context:
            serializer.is_valid(raise_exception=True)
        self.assertIn("Unsupported format", str(context.exception.detail["file"][0]))

        # 2. Size limit Check (10MB limit)
        huge_file = SimpleUploadedFile("big.txt", b"x" * 11000000, content_type="text/plain")
        serializer = KBFilesSerializer(
            data={"file": huge_file},
            context={"request": self.request}
        )
        with self.assertRaises(ValidationError) as context:
            serializer.is_valid(raise_exception=True)
        self.assertIn("File size should be less than", str(context.exception.detail["file"][0]))

        # 3. Valid Upload
        good_file = SimpleUploadedFile("doc.pdf", b"pdf contents", content_type="application/pdf")
        serializer = KBFilesSerializer(
            data={"file": good_file},
            context={"request": self.request}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

        # 4. Valid Upload with extension passed from API
        good_file_2 = SimpleUploadedFile("doc2.pdf", b"pdf contents", content_type="application/pdf")
        serializer_api = KBFilesSerializer(
            data={"file": good_file_2, "extension": "pdf"},
            context={"request": self.request}
        )
        self.assertTrue(serializer_api.is_valid(), serializer_api.errors)
        instance = serializer_api.save(tenant=self.tenant)
        self.assertEqual(instance.extension, ".pdf")

        # 5. Invalid extension passed from API
        good_file_3 = SimpleUploadedFile("doc3.pdf", b"pdf contents", content_type="application/pdf")
        serializer_bad_ext = KBFilesSerializer(
            data={"file": good_file_3, "extension": ".exe"},
            context={"request": self.request}
        )
        with self.assertRaises(ValidationError) as context:
            serializer_bad_ext.is_valid(raise_exception=True)
        self.assertIn("Unsupported format", str(context.exception.detail["extension"][0]))

    def test_kb_website_serializer_multi_tenant_uniqueness(self):
        # Create an active domain website
        KBWebsite.objects.create(
            tenant=self.tenant,
            url="https://acme.org/pricing"
        )

        # Attempting to register the same website URL on the same tenant should fail uniqueness checks
        serializer = KBWebsiteSerializer(
            data={"url": "https://acme.org/pricing"},
            context={"request": self.request}
        )
        with self.assertRaises(ValidationError) as context:
            serializer.is_valid(raise_exception=True)
        self.assertIn("already been registered", str(context.exception.detail["url"][0]))

        # Attempting to register the same URL on another tenant should succeed!
        serializer_other = KBWebsiteSerializer(
            data={"url": "https://acme.org/pricing"},
            context={"request": self.other_request}
        )
        self.assertTrue(serializer_other.is_valid(), serializer_other.errors)

    def test_knowledge_base_serializer_aggregate_hybrid_nesting(self):
        text = KBText.objects.create(tenant=self.tenant, title="Docs Guide")
        
        # 1. Main Serializer Write using UUID string mapping array format
        serializer = KnowledgeBaseSerializer(
            data={"name": "Help Center", "texts": [str(text.id)]},
            context={"request": self.request}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        instance = serializer.save(tenant=self.tenant)
        self.assertIn(text, instance.texts.all())

        # 2. Main Serializer GET reads (should nest the rich data!)
        read_serializer = KnowledgeBaseSerializer(instance, context={"request": self.request})
        nested_texts = read_serializer.data["texts"]
        self.assertEqual(len(nested_texts), 1)
        self.assertEqual(nested_texts[0]["title"], "Docs Guide")

    def test_knowledge_base_serializer_deep_nested_writes(self):
        # 1. Create with fully nested raw payloads
        payload = {
            "name": "Nested Guides",
            "texts": [{"title": "API Setup", "description": "Standard JSON docs"}],
            "websites": [{"url": "https://setup-guides.io"}]
        }
        serializer = KnowledgeBaseSerializer(
            data=payload,
            context={"request": self.request}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        kb = serializer.save(tenant=self.tenant)

        # Assert correct M2M records were dynamically instantiated
        self.assertEqual(kb.texts.count(), 1)
        created_text = kb.texts.first()
        self.assertEqual(created_text.title, "API Setup")
        self.assertEqual(created_text.tenant, self.tenant)

        self.assertEqual(kb.websites.count(), 1)
        created_web = kb.websites.first()
        self.assertEqual(created_web.url, "https://setup-guides.io")

        # 2. Perform deep nested update:
        # We will:
        # - Update the existing text's description using flag 'update'
        # - Delete/disassociate the website using flag 'delete'
        # - Add a new nested Question-Answer element using flag 'create'
        update_payload = {
            "name": "Nested Guides Updated",
            "texts": [{"id": str(created_text.id), "flag": "update", "title": "API Setup", "description": "Updated JSON docs info"}],
            "websites": [{"id": str(created_web.id), "flag": "delete"}],
            "question_answers": [{
                "flag": "create",
                "answer": "Standard nested answer",
                "questions": [{"flag": "create", "question": "What is nested?"}]
            }]
        }
        update_serializer = KnowledgeBaseSerializer(
            kb,
            data=update_payload,
            context={"request": self.request}
        )
        self.assertTrue(update_serializer.is_valid(), update_serializer.errors)
        updated_kb = update_serializer.save(tenant=self.tenant)

        # Assert parent update
        self.assertEqual(updated_kb.name, "Nested Guides Updated")

        # Assert subtext update
        created_text.refresh_from_db()
        self.assertEqual(created_text.description, "Updated JSON docs info")

        # Assert website disassociation and soft-deletion cascade
        self.assertEqual(updated_kb.websites.count(), 0)
        created_web.refresh_from_db()
        self.assertTrue(created_web.is_deleted)

        # Assert new nested Q&A and subquestions creation
        self.assertEqual(updated_kb.question_answers.count(), 1)
        qa_obj = updated_kb.question_answers.first()
        self.assertEqual(qa_obj.answer, "Standard nested answer")
        self.assertEqual(qa_obj.questions.count(), 1)
        self.assertEqual(qa_obj.questions.first().question, "What is nested?")

    def test_knowledge_base_etl_serializer(self):
        # Create a complete KnowledgeBase with associated entities
        text = KBText.objects.create(tenant=self.tenant, title="Doc text", description="some content")
        file_obj = KBFiles.objects.create(tenant=self.tenant, file=SimpleUploadedFile("doc.pdf", b"bytes"), original_name="doc.pdf", extension=".pdf")
        web = KBWebsite.objects.create(tenant=self.tenant, url="https://test.com", content="web content")
        q = KBQuestions.objects.create(tenant=self.tenant, question="A question?")
        qa = KBQuestionAnswer.objects.create(tenant=self.tenant, title="Q&A title", answer="An answer")
        qa.questions.add(q)

        kb = KnowledgeBase.objects.create(tenant=self.tenant, name="ETL test KB")
        kb.texts.add(text)
        kb.files.add(file_obj)
        kb.websites.add(web)
        kb.question_answers.add(qa)

        # Serialize
        serializer = KnowledgeBaseETLSerializer(kb)
        data = serializer.data

        # Check fields
        self.assertEqual(data["id"], str(kb.id))
        self.assertEqual(data["name"], "ETL test KB")
        self.assertEqual(data["is_processed"], False)
        self.assertEqual(data["is_deleted"], False)
        self.assertEqual(data["tenant"]["id"], str(self.tenant.id))

        # Check nested lists
        self.assertEqual(len(data["texts"]), 1)
        self.assertEqual(data["texts"][0]["title"], "Doc text")
        self.assertEqual(data["texts"][0]["description"], "some content")

        self.assertEqual(len(data["files"]), 1)
        self.assertEqual(data["files"][0]["original_name"], "doc.pdf")
        self.assertEqual(data["files"][0]["extension"], ".pdf")

        self.assertEqual(len(data["websites"]), 1)
        self.assertEqual(data["websites"][0]["url"], "https://test.com")

        self.assertEqual(len(data["question_answers"]), 1)
        self.assertEqual(data["question_answers"][0]["answer"], "An answer")

    @patch('utils.tasks.trigger_kb_etl_task.delay')
    def test_knowledge_base_serializer_create_triggers_etl(self, mock_delay):
        # Payload to create KnowledgeBase with nested components
        payload = {
            "name": "Trigger Test KB",
            "texts": [{"title": "Text Title", "description": "Text content"}],
            "files": [],
            "websites": [{"url": "https://trigger.com", "content": "Trigger website content"}],
            "question_answers": []
        }

        serializer = KnowledgeBaseSerializer(
            data=payload,
            context={"request": self.request}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        
        with self.captureOnCommitCallbacks(execute=True):
            kb = serializer.save(tenant=self.tenant)

        # Check mock delay was called exactly once
        self.assertEqual(mock_delay.call_count, 1)
        mock_delay.assert_called_once_with(str(kb.id), str(kb.etl_id))

        # Verify KB status
        kb.refresh_from_db()
        self.assertIsNotNone(kb.etl_id)
        self.assertEqual(kb.processing_status, "PENDING")

    @patch('requests.post')
    def test_trigger_kb_etl_task_success(self, mock_post):
        from unittest.mock import MagicMock
        import uuid
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "ok"}
        mock_post.return_value = mock_response

        from utils.tasks import trigger_kb_etl_task
        etl_id = uuid.uuid4()
        kb = KnowledgeBase.objects.create(tenant=self.tenant, name="Celery Success KB", etl_id=etl_id)

        trigger_kb_etl_task(str(kb.id), str(etl_id))

        # Check mock post was called
        self.assertEqual(mock_post.call_count, 1)
        call_kwargs = mock_post.call_args[1]
        self.assertIn("json", call_kwargs)
        post_data = call_kwargs["json"]
        self.assertEqual(post_data["payload"]["name"], "Celery Success KB")

    @patch('requests.post')
    def test_trigger_kb_etl_task_failure(self, mock_post):
        mock_post.side_effect = Exception("Connection refused")

        import uuid
        from utils.tasks import trigger_kb_etl_task
        etl_id = uuid.uuid4()
        kb = KnowledgeBase.objects.create(tenant=self.tenant, name="Celery Failure KB", etl_id=etl_id)

        from celery.exceptions import MaxRetriesExceededError
        from unittest.mock import MagicMock
        
        retry_mock = MagicMock(side_effect=MaxRetriesExceededError())
        
        with patch.object(trigger_kb_etl_task, 'retry', retry_mock):
            trigger_kb_etl_task(str(kb.id), str(etl_id))

        kb.refresh_from_db()
        self.assertEqual(kb.processing_status, "FAILED")
        self.assertIn("Failed to trigger ingestion", kb.processing_error)

    def test_trigger_kb_etl_task_stale_skipped(self):
        """Verify that task execution is skipped if a newer etl_id has been set in the DB"""
        from utils.tasks import trigger_kb_etl_task
        import uuid
        old_etl_id = uuid.uuid4()
        kb = KnowledgeBase.objects.create(tenant=self.tenant, name="Stale KB", etl_id=old_etl_id)
        
        kb.etl_id = uuid.uuid4()
        kb.save()

        from unittest.mock import patch
        with patch('requests.post') as mock_post:
            trigger_kb_etl_task(str(kb.id), str(old_etl_id))
            mock_post.assert_not_called()
