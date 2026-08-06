"""
End-to-end staged contact import: upload -> parse -> review -> resolve -> commit.

Test settings run Celery eagerly and back FileField with a temp FileSystemStorage,
so these exercise the real parse and commit code paths rather than mocks.
"""

from django.core.files.base import ContentFile
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.contact import Contact
from core.models.contact_import import (
    ContactImportFile,
    ContactImportRow,
    ContactImportSession,
)
from core.services import contact_import as import_service
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

IMPORTS = "/api/v1/core/contact-imports/"
ROWS = "/api/v1/core/contact-import-rows/"

Status = ContactImportSession.Status
RowStatus = ContactImportRow.Status
Resolution = ContactImportRow.Resolution


@override_settings(CONTACT_DEFAULT_PHONE_REGION="US")
class ImportTestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-imp")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-imp")

        self.user = User.objects.create_user(
            username="alex", email="alex@example.com", password="password"
        )
        self.colleague = User.objects.create_user(
            username="casey", email="casey@example.com", password="password"
        )
        self.outsider = User.objects.create_user(
            username="bob", email="bob@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.colleague, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.outsider, tenant=self.other_tenant)

        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)
        self.client_casey = APIClient()
        self.client_casey.force_authenticate(user=self.colleague)
        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.outsider)

    # -- helpers ---------------------------------------------------------

    def make_file(self, content, name="contacts.csv", tenant=None, confirmed=True):
        if isinstance(content, str):
            content = content.encode("utf-8")

        import_file = ContactImportFile(
            tenant=tenant or self.tenant,
            original_name=name,
            extension=".csv",
            created_by=self.user,
            upload_status=(
                ContactImportFile.UploadStatus.CONFIRMED
                if confirmed
                else ContactImportFile.UploadStatus.PENDING
            ),
        )
        import_file.file.save(name, ContentFile(content), save=False)
        import_file.file_size_in_bytes = len(content)
        import_file.save()
        return import_file

    def start_import(self, content, client=None, **payload):
        import_file = self.make_file(content)
        client = client or self.client_alex
        body = {"file_id": str(import_file.id), **payload}
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(IMPORTS, body, format="json")
        return response

    def session_from(self, response):
        return ContactImportSession.objects.get(id=response.data["data"]["id"])

    def rows_of(self, session):
        return list(
            ContactImportRow.all_objects.filter(session=session).order_by("row_number")
        )

    def commit(self, session, client=None, **payload):
        client = client or self.client_alex
        with self.captureOnCommitCallbacks(execute=True):
            return client.post(f"{IMPORTS}{session.id}/commit/", payload, format="json")


class StartImportTests(ImportTestCase):
    CSV = "first_name,phone_number\nAda,+14155552671\n"

    @override_settings(CONTACT_DEFAULT_PHONE_REGION="")
    def test_missing_region_is_rejected(self):
        """D11: a region must be present, or the request is an error."""
        import_file = self.make_file(self.CSV)
        response = self.client_alex.post(
            IMPORTS, {"file_id": str(import_file.id)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("default_region", response.data["errors"])
        self.assertFalse(ContactImportSession.objects.exists())

    @override_settings(CONTACT_DEFAULT_PHONE_REGION="")
    def test_explicit_region_satisfies_the_requirement(self):
        response = self.start_import(self.CSV, default_region="US")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_unrecognised_region_is_rejected(self):
        import_file = self.make_file(self.CSV)
        response = self.client_alex.post(
            IMPORTS,
            {"file_id": str(import_file.id), "default_region": "ZZ"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unconfirmed_file_is_rejected(self):
        import_file = self.make_file(self.CSV, confirmed=False)
        response = self.client_alex.post(
            IMPORTS, {"file_id": str(import_file.id)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_tenants_file_is_not_found(self):
        import_file = self.make_file(self.CSV, tenant=self.other_tenant)
        response = self.client_alex.post(
            IMPORTS, {"file_id": str(import_file.id)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_one_file_backs_only_one_session(self):
        import_file = self.make_file(self.CSV)
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client_alex.post(
                IMPORTS, {"file_id": str(import_file.id)}, format="json"
            )
        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED)

        second = self.client_alex.post(
            IMPORTS, {"file_id": str(import_file.id)}, format="json"
        )
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(CONTACT_IMPORT_MAX_OPEN_SESSIONS=1)
    def test_open_session_cap(self):
        """Sessions parse eagerly here, so leave one READY to occupy the slot."""
        self.start_import(self.CSV)
        response = self.start_import("first_name,phone_number\nB,+14155552699\n")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ParseTests(ImportTestCase):
    def test_parses_and_flags_every_conflict_type(self):
        Contact.objects.create(tenant=self.tenant, phone_number="+14155550001")
        archived = Contact.objects.create(
            tenant=self.tenant, phone_number="+14155550002"
        )
        archived.soft_delete()

        csv_text = (
            "first_name,last_name,phone_number,email_address,company,tags,notes\n"
            "New,Person,+14155550100,new@example.com,Acme,vip|lead,hello\n"
            "Existing,Person,+14155550001,,,,\n"
            "Dup,One,+14155550200,,,,\n"
            "Dup,Two,+14155550200,,,,\n"
            "Archived,Person,+14155550002,,,,\n"
            "Bad,Number,notaphone,,,,\n"
            "Missing,Phone,,,,,\n"
        )
        response = self.start_import(csv_text)
        session = self.session_from(response)
        session.refresh_from_db()

        self.assertEqual(session.status, Status.READY)
        self.assertEqual(session.total_rows, 7)

        by_status = {}
        for row in self.rows_of(session):
            by_status.setdefault(row.status, []).append(row.row_number)

        # Row 4 is the first occurrence of the duplicated number, so it stays valid;
        # only the second occurrence (row 5) is flagged.
        self.assertEqual(by_status[RowStatus.PENDING], [2, 4])
        self.assertEqual(by_status[RowStatus.CONFLICT_EXISTING], [3])
        self.assertEqual(by_status[RowStatus.CONFLICT_DUPLICATE], [5])
        self.assertEqual(by_status[RowStatus.CONFLICT_ARCHIVED], [6])
        self.assertEqual(sorted(by_status[RowStatus.INVALID]), [7, 8])

        self.assertEqual(session.invalid_rows, 2)
        self.assertEqual(session.conflict_rows, 3)

    def test_parses_bom_and_semicolon_delimited_file(self):
        """Excel in many locales writes UTF-8-BOM with semicolons."""
        csv_text = "﻿first_name;phone_number\nAda;+14155550300\n"
        response = self.start_import(csv_text.encode("utf-8"))
        session = self.session_from(response)
        session.refresh_from_db()

        self.assertEqual(session.status, Status.READY)
        rows = self.rows_of(session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].parsed_data["first_name"], "Ada")
        self.assertEqual(rows[0].phone_e164, "+14155550300")

    def test_header_aliases_are_accepted(self):
        csv_text = "First Name,Surname,Mobile,E-Mail,Organisation\nAda,L,+14155550400,a@b.com,Acme\n"
        response = self.start_import(csv_text)
        session = self.session_from(response)
        session.refresh_from_db()

        self.assertEqual(session.status, Status.READY)
        row = self.rows_of(session)[0]
        self.assertEqual(row.parsed_data["first_name"], "Ada")
        self.assertEqual(row.parsed_data["last_name"], "L")
        self.assertEqual(row.parsed_data["email_address"], "a@b.com")
        self.assertEqual(row.parsed_data["company"], "Acme")

    def test_quoted_fields_with_commas_and_newlines(self):
        csv_text = (
            'first_name,phone_number,notes\n'
            '"Ada",+14155550500,"Line one\nLine two, with comma"\n'
        )
        response = self.start_import(csv_text)
        session = self.session_from(response)
        rows = self.rows_of(session)
        self.assertEqual(len(rows), 1)
        self.assertIn("with comma", rows[0].parsed_data["notes"])

    def test_missing_required_column_fails_the_session(self):
        response = self.start_import("first_name,company\nAda,Acme\n")
        session = self.session_from(response)
        session.refresh_from_db()

        self.assertEqual(session.status, Status.FAILED)
        self.assertIn("phone_number", session.error_message)
        self.assertEqual(ContactImportRow.all_objects.filter(session=session).count(), 0)

    @override_settings(CONTACT_IMPORT_MAX_ROWS=2)
    def test_row_cap_fails_the_session(self):
        csv_text = "phone_number\n+14155550601\n+14155550602\n+14155550603\n"
        response = self.start_import(csv_text)
        session = self.session_from(response)
        session.refresh_from_db()
        self.assertEqual(session.status, Status.FAILED)

    def test_consent_columns_are_ignored_with_a_notice(self):
        """Re-uploading a stale list must not resurrect an opt-out."""
        csv_text = "phone_number,do_not_call,consent_status\n+14155550700,false,GRANTED\n"
        response = self.start_import(csv_text)
        session = self.session_from(response)
        session.refresh_from_db()

        self.assertEqual(session.status, Status.READY)
        self.assertTrue(any("do_not_call" in n for n in session.notices))

        row = self.rows_of(session)[0]
        self.assertNotIn("do_not_call", row.parsed_data)
        self.assertNotIn("consent_status", row.parsed_data)

    def test_blank_lines_are_skipped(self):
        csv_text = "phone_number\n+14155550801\n\n\n+14155550802\n"
        response = self.start_import(csv_text)
        session = self.session_from(response)
        session.refresh_from_db()
        self.assertEqual(session.total_rows, 2)

    def test_tags_are_pipe_delimited(self):
        csv_text = "phone_number,tags\n+14155550900,vip|lead|vip\n"
        response = self.start_import(csv_text)
        row = self.rows_of(self.session_from(response))[0]
        self.assertEqual(row.parsed_data["tags"], ["vip", "lead"])


class RowReviewTests(ImportTestCase):
    def setUp(self):
        super().setUp()
        Contact.objects.create(tenant=self.tenant, phone_number="+14155551001")
        response = self.start_import(
            "first_name,phone_number\nTaken,+14155551001\nFree,+14155551002\n"
        )
        self.session = self.session_from(response)
        self.conflict_row = ContactImportRow.all_objects.get(
            session=self.session, status=RowStatus.CONFLICT_EXISTING
        )

    def test_rows_listing_filters_by_status(self):
        response = self.client_alex.get(
            f"{IMPORTS}{self.session.id}/rows/", {"status": "CONFLICT_EXISTING"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)

    def test_client_cannot_set_status_directly(self):
        """D4: marking a row resolved without fixing it would blow up at commit."""
        response = self.client_alex.patch(
            f"{ROWS}{self.conflict_row.id}/",
            {"status": "RESOLVED"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.conflict_row.refresh_from_db()
        self.assertEqual(self.conflict_row.status, RowStatus.CONFLICT_EXISTING)

    def test_editing_to_a_free_number_clears_the_conflict(self):
        response = self.client_alex.patch(
            f"{ROWS}{self.conflict_row.id}/",
            {"parsed_data": {"phone_number": "+14155551999"}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.conflict_row.refresh_from_db()
        self.assertEqual(self.conflict_row.status, RowStatus.PENDING)
        self.assertEqual(self.conflict_row.phone_e164, "+14155551999")

    def test_editing_to_another_taken_number_stays_conflicted(self):
        Contact.objects.create(tenant=self.tenant, phone_number="+14155551500")
        self.client_alex.patch(
            f"{ROWS}{self.conflict_row.id}/",
            {"parsed_data": {"phone_number": "+14155551500"}},
            format="json",
        )
        self.conflict_row.refresh_from_db()
        self.assertEqual(self.conflict_row.status, RowStatus.CONFLICT_EXISTING)

    def test_editing_to_a_bad_number_marks_invalid(self):
        self.client_alex.patch(
            f"{ROWS}{self.conflict_row.id}/",
            {"parsed_data": {"phone_number": "notaphone"}},
            format="json",
        )
        self.conflict_row.refresh_from_db()
        self.assertEqual(self.conflict_row.status, RowStatus.INVALID)

    def test_choosing_skip_marks_the_row_skipped(self):
        self.client_alex.patch(
            f"{ROWS}{self.conflict_row.id}/", {"resolution": "SKIP"}, format="json"
        )
        self.conflict_row.refresh_from_db()
        self.assertEqual(self.conflict_row.status, RowStatus.SKIPPED)

    def test_choosing_update_marks_the_row_resolved(self):
        self.client_alex.patch(
            f"{ROWS}{self.conflict_row.id}/", {"resolution": "UPDATE"}, format="json"
        )
        self.conflict_row.refresh_from_db()
        self.assertEqual(self.conflict_row.status, RowStatus.RESOLVED)

    def test_bulk_resolve(self):
        response = self.client_alex.post(
            f"{IMPORTS}{self.session.id}/bulk-resolve/",
            {"status": "CONFLICT_EXISTING", "resolution": "SKIP"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["updated"], 1)

        self.conflict_row.refresh_from_db()
        self.assertEqual(self.conflict_row.status, RowStatus.SKIPPED)

    def test_row_edit_rejected_once_session_is_not_ready(self):
        self.session.status = Status.COMPLETED
        self.session.save(update_fields=["status"])

        response = self.client_alex.patch(
            f"{ROWS}{self.conflict_row.id}/",
            {"resolution": "SKIP"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_rows_are_not_visible_to_another_tenant(self):
        response = self.client_bob.get(f"{ROWS}{self.conflict_row.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class CommitTests(ImportTestCase):
    def test_commit_creates_contacts_and_purges_staging(self):
        response = self.start_import(
            "first_name,last_name,phone_number,tags\n"
            "Ada,Lovelace,+14155552001,vip|lead\n"
            "Grace,Hopper,+14155552002,\n"
        )
        session = self.session_from(response)

        commit = self.commit(session)
        self.assertEqual(commit.status_code, status.HTTP_202_ACCEPTED)

        session.refresh_from_db()
        self.assertEqual(session.status, Status.COMPLETED)
        self.assertEqual(session.created_count, 2)

        contacts = Contact.objects.filter(tenant=self.tenant).order_by("phone_e164")
        self.assertEqual(contacts.count(), 2)
        self.assertEqual(contacts[0].source, Contact.Source.IMPORT)
        self.assertEqual(contacts[0].tags, ["vip", "lead"])
        self.assertEqual(contacts[0].created_by_id, self.user.id)

        # Staging rows are purged; the session survives as an audit record.
        self.assertEqual(ContactImportRow.all_objects.filter(session=session).count(), 0)
        self.assertTrue(ContactImportSession.objects.filter(id=session.id).exists())

    def test_commit_refuses_while_conflicts_remain(self):
        Contact.objects.create(tenant=self.tenant, phone_number="+14155552100")
        response = self.start_import(
            "phone_number\n+14155552100\n+14155552101\n"
        )
        session = self.session_from(response)

        commit = self.commit(session)
        self.assertEqual(commit.status_code, status.HTTP_409_CONFLICT)

        session.refresh_from_db()
        self.assertEqual(session.status, Status.READY)

    def test_commit_with_ignore_unresolved_skips_conflicts(self):
        Contact.objects.create(tenant=self.tenant, phone_number="+14155552200")
        response = self.start_import(
            "phone_number\n+14155552200\n+14155552201\n"
        )
        session = self.session_from(response)

        commit = self.commit(session, ignore_unresolved=True)
        self.assertEqual(commit.status_code, status.HTTP_202_ACCEPTED)

        session.refresh_from_db()
        self.assertEqual(session.status, Status.COMPLETED)
        self.assertEqual(session.created_count, 1)
        self.assertEqual(session.skipped_count, 1)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 2)

    def test_double_commit_is_rejected(self):
        response = self.start_import("phone_number\n+14155552300\n")
        session = self.session_from(response)

        first = self.commit(session)
        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED)

        second = self.commit(session)
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)

        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 1)

    def test_contact_created_between_parse_and_commit_does_not_duplicate(self):
        """
        TOCTOU: parse-time flags are stale by commit time, so conflicts are
        re-checked inside the commit transaction.
        """
        response = self.start_import("phone_number\n+14155552400\n")
        session = self.session_from(response)

        row = self.rows_of(session)[0]
        self.assertEqual(row.status, RowStatus.PENDING)

        # Someone else creates the same contact in the meantime.
        Contact.objects.create(tenant=self.tenant, phone_number="+14155552400")

        commit = self.commit(session)
        self.assertEqual(commit.status_code, status.HTTP_202_ACCEPTED)

        session.refresh_from_db()
        self.assertEqual(session.created_count, 0)
        self.assertEqual(session.skipped_count, 1)
        self.assertEqual(session.failed_count, 0)
        self.assertEqual(
            Contact.objects.filter(
                tenant=self.tenant, phone_e164="+14155552400"
            ).count(),
            1,
        )


class UpdateResolutionTests(ImportTestCase):
    """The three rails on D9's UPDATE path."""

    def setUp(self):
        super().setUp()
        self.existing = Contact.objects.create(
            tenant=self.tenant,
            first_name="Ada",
            phone_number="+14155553001",
            company="Original Co",
            email_address="ada@old.com",
            consent_status=Contact.ConsentStatus.WITHDRAWN,
            do_not_call=True,
            opted_out_at=timezone.now(),
        )

    def _import_and_update(self, csv_text):
        response = self.start_import(csv_text)
        session = self.session_from(response)
        self.client_alex.post(
            f"{IMPORTS}{session.id}/bulk-resolve/",
            {"status": "CONFLICT_EXISTING", "resolution": "UPDATE"},
            format="json",
        )
        self.commit(session)
        session.refresh_from_db()
        self.existing.refresh_from_db()
        return session

    def test_update_mutates_the_existing_contact_without_duplicating(self):
        session = self._import_and_update(
            "first_name,phone_number,company\nAda,+14155553001,New Co\n"
        )
        self.assertEqual(session.updated_count, 1)
        self.assertEqual(session.created_count, 0)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(self.existing.company, "New Co")

    def test_blank_cells_do_not_wipe_populated_fields(self):
        """The classic bulk-import data-loss bug."""
        self._import_and_update(
            "first_name,phone_number,company,email_address\nAda,+14155553001,,\n"
        )
        self.assertEqual(self.existing.company, "Original Co")
        self.assertEqual(self.existing.email_address, "ada@old.com")

    def test_import_cannot_clear_an_opt_out(self):
        """Re-uploading a stale list must not resurrect someone who opted out."""
        self._import_and_update(
            "phone_number,do_not_call,consent_status\n+14155553001,false,GRANTED\n"
        )
        self.assertTrue(self.existing.do_not_call)
        self.assertEqual(self.existing.consent_status, Contact.ConsentStatus.WITHDRAWN)
        self.assertIsNotNone(self.existing.opted_out_at)

    def test_default_strategy_skips_rather_than_overwrites(self):
        """Nothing is overwritten unless UPDATE is explicitly chosen."""
        response = self.start_import(
            "first_name,phone_number,company\nAda,+14155553001,Should Not Apply\n"
        )
        session = self.session_from(response)
        self.assertEqual(session.conflict_strategy, ContactImportSession.ConflictStrategy.SKIP)

        self.commit(session, ignore_unresolved=True)
        session.refresh_from_db()
        self.existing.refresh_from_db()

        self.assertEqual(session.updated_count, 0)
        self.assertEqual(self.existing.company, "Original Co")


class TenantWideAccessTests(ImportTestCase):
    """D10: sessions are shared across the tenant, with attribution."""

    def setUp(self):
        super().setUp()
        response = self.start_import("phone_number\n+14155554001\n")
        self.session = self.session_from(response)

    def test_colleague_can_see_and_commit_another_users_session(self):
        listing = self.client_casey.get(IMPORTS)
        self.assertEqual(len(listing.data["data"]), 1)

        commit = self.commit(self.session, client=self.client_casey)
        self.assertEqual(commit.status_code, status.HTTP_202_ACCEPTED)

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, Status.COMPLETED)
        # Started by one colleague, committed by another — both recorded.
        self.assertEqual(self.session.created_by_id, self.user.id)
        self.assertEqual(self.session.committed_by_id, self.colleague.id)

    def test_row_edits_record_the_editor(self):
        row = self.rows_of(self.session)[0]
        self.client_casey.patch(
            f"{ROWS}{row.id}/", {"resolution": "SKIP"}, format="json"
        )
        row.refresh_from_db()
        self.assertEqual(row.updated_by_id, self.colleague.id)

    def test_another_tenant_sees_nothing(self):
        listing = self.client_bob.get(IMPORTS)
        self.assertEqual(len(listing.data["data"]), 0)

        detail = self.client_bob.get(f"{IMPORTS}{self.session.id}/")
        self.assertEqual(detail.status_code, status.HTTP_404_NOT_FOUND)

        commit = self.client_bob.post(f"{IMPORTS}{self.session.id}/commit/", {}, format="json")
        self.assertEqual(commit.status_code, status.HTTP_404_NOT_FOUND)


class DiscardAndCleanupTests(ImportTestCase):
    def test_discard_purges_rows_and_keeps_the_session(self):
        response = self.start_import("phone_number\n+14155555001\n")
        session = self.session_from(response)

        discard = self.client_alex.post(f"{IMPORTS}{session.id}/discard/", {}, format="json")
        self.assertEqual(discard.status_code, status.HTTP_200_OK)

        session.refresh_from_db()
        self.assertEqual(session.status, Status.DISCARDED)
        self.assertEqual(session.discarded_by_id, self.user.id)
        self.assertEqual(ContactImportRow.all_objects.filter(session=session).count(), 0)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 0)

    def test_discarding_twice_is_rejected(self):
        response = self.start_import("phone_number\n+14155555002\n")
        session = self.session_from(response)

        self.client_alex.post(f"{IMPORTS}{session.id}/discard/", {}, format="json")
        second = self.client_alex.post(f"{IMPORTS}{session.id}/discard/", {}, format="json")
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)

    def test_expired_sessions_are_purged_and_marked(self):
        response = self.start_import("phone_number\n+14155555003\n")
        session = self.session_from(response)

        ContactImportSession.all_objects.filter(id=session.id).update(
            expires_at=timezone.now() - timezone.timedelta(hours=1)
        )

        purged = import_service.cleanup_expired_sessions()
        self.assertEqual(purged, 1)

        session.refresh_from_db()
        self.assertEqual(session.status, Status.EXPIRED)
        self.assertEqual(ContactImportRow.all_objects.filter(session=session).count(), 0)

    def test_cleanup_ignores_healthy_sessions(self):
        response = self.start_import("phone_number\n+14155555004\n")
        session = self.session_from(response)

        self.assertEqual(import_service.cleanup_expired_sessions(), 0)
        session.refresh_from_db()
        self.assertEqual(session.status, Status.READY)

    def test_expired_session_cannot_be_committed(self):
        response = self.start_import("phone_number\n+14155555005\n")
        session = self.session_from(response)

        ContactImportSession.all_objects.filter(id=session.id).update(
            expires_at=timezone.now() - timezone.timedelta(hours=1)
        )

        commit = self.commit(session)
        self.assertEqual(commit.status_code, status.HTTP_409_CONFLICT)

    def test_s3_delete_failure_does_not_block_cleanup(self):
        """A failed blob delete must not roll back the DB purge or wedge the sweep."""
        from unittest.mock import patch

        response = self.start_import("phone_number\n+14155555006\n")
        session = self.session_from(response)
        ContactImportSession.all_objects.filter(id=session.id).update(
            expires_at=timezone.now() - timezone.timedelta(hours=1)
        )

        with patch(
            "utils.s3_storage.service.S3StorageService.delete_blob_direct",
            side_effect=Exception("bucket unreachable"),
        ):
            purged = import_service.cleanup_expired_sessions()

        self.assertEqual(purged, 1)
        session.refresh_from_db()
        self.assertEqual(session.status, Status.EXPIRED)
        self.assertEqual(ContactImportRow.all_objects.filter(session=session).count(), 0)


class UploadEndpointTests(ImportTestCase):
    def test_template_download(self):
        response = self.client_alex.get(f"{IMPORTS}template/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.content.decode()
        self.assertIn("phone_number", body)
        self.assertIn("vip|lead", body)

    def test_confirm_upload_rejects_another_tenants_file(self):
        """The object key never leaves the server, and file_id is tenant-scoped."""
        import_file = self.make_file(
            "phone_number\n+14155556001\n", tenant=self.other_tenant, confirmed=False
        )
        response = self.client_alex.post(
            f"{IMPORTS}confirm-upload/", {"file_id": str(import_file.id)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_request_upload_url_rejects_non_csv(self):
        response = self.client_alex.post(
            f"{IMPORTS}request-upload-url/",
            {
                "filename": "contacts.exe",
                "content_type": "text/csv",
                "file_size_in_bytes": 100,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(CONTACT_IMPORT_MAX_FILE_BYTES=1024)
    def test_request_upload_url_rejects_oversize(self):
        response = self.client_alex.post(
            f"{IMPORTS}request-upload-url/",
            {
                "filename": "contacts.csv",
                "content_type": "text/csv",
                "file_size_in_bytes": 999999,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_request_upload_url_creates_pending_file_and_sanitizes_the_key(self):
        """A hostile filename must not escape the tenant's prefix.

        `object_key` IS returned to the client, but it is never client-controlled:
        it is derived from the row's own id and a sanitized filename, so the
        traversal attempt below lands inside `t-<tenant>/contact_imports/` like
        any other upload. Returning it is informational — `confirm-upload` accepts
        `file_id` only and ignores any key a client sends back, so there is no
        path where this value is trusted as input.
        """
        from unittest.mock import patch

        with patch(
            "utils.s3_storage.service.S3StorageService.get_presigned_put_url",
            return_value="https://example/put",
        ):
            response = self.client_alex.post(
                f"{IMPORTS}request-upload-url/",
                {
                    "filename": "../../evil.csv",
                    "content_type": "text/csv",
                    "file_size_in_bytes": 100,
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("file_id", response.data["data"])
        self.assertIn("object_key", response.data["data"])

        import_file = ContactImportFile.objects.get(id=response.data["data"]["file_id"])
        self.assertEqual(import_file.upload_status, ContactImportFile.UploadStatus.PENDING)
        self.assertTrue(import_file.file.name.startswith(f"t-{self.tenant.id}/contact_imports/"))
        self.assertNotIn("..", import_file.file.name)
        # What is returned is exactly what was stored — no second, unsanitized copy.
        self.assertEqual(response.data["data"]["object_key"], import_file.file.name)
        self.assertNotIn("..", response.data["data"]["object_key"])

    def test_confirm_upload_ignores_a_client_supplied_object_key(self):
        """The returned key must not become a trusted input.

        This is the failure the key-hiding once guarded against: a client passing
        someone else's key back at confirm time. `confirm-upload` resolves the
        file by `file_id` alone, so an injected `object_key` changes nothing.
        """
        from unittest.mock import patch

        with patch(
            "utils.s3_storage.service.S3StorageService.get_presigned_put_url",
            return_value="https://example/put",
        ):
            created = self.client_alex.post(
                f"{IMPORTS}request-upload-url/",
                {
                    "filename": "contacts.csv",
                    "content_type": "text/csv",
                    "file_size_in_bytes": 100,
                },
                format="json",
            )
        file_id = created.data["data"]["file_id"]

        with patch(
            "utils.s3_storage.service.S3StorageService.verify_object_exists",
            return_value=True,
        ), patch(
            "utils.s3_storage.service.S3StorageService.get_file_disk_usage",
            return_value={"total_size_bytes": 100},
        ) as usage:
            self.client_alex.post(
                f"{IMPORTS}confirm-upload/",
                {"file_id": file_id, "object_key": "t-other-tenant/contact_imports/theirs.csv"},
                format="json",
            )

        import_file = ContactImportFile.objects.get(id=file_id)
        # The stored key was used, not the one the client tried to inject.
        usage.assert_called_once_with(import_file.file.name)
        self.assertTrue(import_file.file.name.startswith(f"t-{self.tenant.id}/"))
