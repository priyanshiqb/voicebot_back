from django.test import TestCase
from django.db import IntegrityError
from django.core.files.uploadedfile import SimpleUploadedFile
from tenant.models.Tenant import Tenant
from core.models.knowledge_base import (
    KBText,
    KBFiles,
    KBQuestions,
    KBQuestionAnswer,
    KBWebsite,
    KnowledgeBase
)
from core.models.kb_etl_tracker import KBETLTracker

class KnowledgeBaseTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Alpha Corp",
            subdomain="alpha-test"
        )
        self.other_tenant = Tenant.objects.create(
            name="Beta Corp",
            subdomain="beta-test"
        )

    def test_kb_text_creation(self):
        text = KBText.objects.create(
            tenant=self.tenant,
            title="Text block title",
            description="Detailed description content."
        )
        self.assertEqual(text.title, "Text block title")
        self.assertEqual(text.tenant, self.tenant)
        self.assertFalse(text.is_processed)
        self.assertEqual(text.processing_status, KBText.ProcessingStatusChoices.PENDING)
        self.assertEqual(text.processing_history, [])

    def test_kb_files_creation(self):
        dummy_file = SimpleUploadedFile("dummy_doc.txt", b"file contents bytes", content_type="text/plain")
        file_obj = KBFiles.objects.create(
            tenant=self.tenant,
            file=dummy_file
        )
        self.assertTrue(file_obj.file.name.endswith("dummy_doc.txt"))
        self.assertEqual(file_obj.original_name, "dummy_doc.txt")
        self.assertEqual(file_obj.file_size_in_bytes, 19)
        self.assertEqual(str(file_obj), "dummy_doc.txt")

    def test_kb_qna_creation(self):
        q1 = KBQuestions.objects.create(
            tenant=self.tenant,
            question="What is the refund deadline?"
        )
        q2 = KBQuestions.objects.create(
            tenant=self.tenant,
            question="Can I request a refund?"
        )
        qna = KBQuestionAnswer.objects.create(
            tenant=self.tenant,
            title="Refund Guidelines Q&A",
            answer="Refunds are supported within 14 days of purchase."
        )
        qna.questions.add(q1, q2)

        self.assertEqual(qna.questions.count(), 2)
        self.assertEqual(qna.answer, "Refunds are supported within 14 days of purchase.")

    def test_kb_website_uniqueness_constraint(self):
        from django.db import transaction
        KBWebsite.objects.create(
            tenant=self.tenant,
            url="https://example.com/faq"
        )
        # Duplicate URL for same tenant should raise IntegrityError
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                KBWebsite.objects.create(
                    tenant=self.tenant,
                    url="https://example.com/faq"
                )
        # Same URL for a different tenant should succeed cleanly
        other_web = KBWebsite.objects.create(
            tenant=self.other_tenant,
            url="https://example.com/faq"
        )
        self.assertEqual(other_web.tenant, self.other_tenant)

    def test_kb_website_soft_delete_suffixing(self):
        web = KBWebsite.objects.create(
            tenant=self.tenant,
            url="https://example.com/faq"
        )
        web.soft_delete()
        self.assertTrue(web.is_deleted)
        self.assertIn("-deleted-", web.url)

        # We can now create a new website with the exact same url on the same tenant without IntegrityError!
        new_web = KBWebsite.objects.create(
            tenant=self.tenant,
            url="https://example.com/faq"
        )
        self.assertEqual(new_web.url, "https://example.com/faq")

    def test_knowledge_base_cascading_soft_delete(self):
        text = KBText.objects.create(tenant=self.tenant, title="Cascaded Guide")
        web = KBWebsite.objects.create(tenant=self.tenant, url="https://cascade.com")
        
        kb = KnowledgeBase.objects.create(
            tenant=self.tenant,
            name="Cascade Bot KB"
        )
        kb.texts.add(text)
        kb.websites.add(web)

        # Call cascading soft delete
        kb.soft_delete()
        kb.refresh_from_db()

        self.assertTrue(kb.is_deleted)
        self.assertIn("-deleted-", kb.name)

        # Verify cascades
        text.refresh_from_db()
        web.refresh_from_db()
        self.assertTrue(text.is_deleted)
        self.assertTrue(web.is_deleted)

    def test_kb_files_orphan_mitigation(self):
        dummy_file_1 = SimpleUploadedFile("doc1.txt", b"doc 1 contents", content_type="text/plain")
        dummy_file_2 = SimpleUploadedFile("doc2.txt", b"doc 2 contents", content_type="text/plain")

        file_obj = KBFiles.objects.create(
            tenant=self.tenant,
            file=dummy_file_1
        )
        # Save old filename
        old_file_name = file_obj.file.name

        # Update file field
        file_obj.file = dummy_file_2
        file_obj.save()

        # Assert old file is gone from storage
        from django.core.files.storage import default_storage
        self.assertFalse(default_storage.exists(old_file_name))

    def test_kb_files_tenant_storage_stats(self):
        dummy_file_1 = SimpleUploadedFile("doc1.txt", b"doc 1 contents", content_type="text/plain")
        dummy_file_2 = SimpleUploadedFile("doc2.txt", b"doc 2 contents", content_type="text/plain")

        KBFiles.objects.create(tenant=self.tenant, file=dummy_file_1)
        f2 = KBFiles.objects.create(tenant=self.tenant, file=dummy_file_2)
        f2.soft_delete()

        stats = KBFiles.get_tenant_file_stats(self.tenant.id)
        self.assertEqual(stats["total_files"], 2)
        self.assertEqual(stats["active_files"], 1)
        self.assertEqual(stats["deleted_files"], 1)

    def test_kb_etl_tracker_creation(self):
        kb = KnowledgeBase.objects.create(
            tenant=self.tenant,
            name="ETL Bot KB"
        )
        tracker = KBETLTracker.objects.create(
            tenant=self.tenant,
            kb=kb,
            task_id="task-123",
            status=KBETLTracker.OverallStatus.PENDING,
            kb_json_snapshot={"name": "ETL Bot KB"},
            to_process_ids={"files": []},
            processed_ids=[],
            processing_stats={"total_items": 0, "success_count": 0, "failure_count": 0},
            error_details={}
        )
        self.assertEqual(tracker.kb, kb)
        self.assertEqual(tracker.status, KBETLTracker.OverallStatus.PENDING)
        self.assertEqual(tracker.task_id, "task-123")
        self.assertEqual(tracker.kb_json_snapshot["name"], "ETL Bot KB")
        self.assertEqual(str(tracker), f"Run {tracker.id} | KB: ETL Bot KB | Status: PENDING")
