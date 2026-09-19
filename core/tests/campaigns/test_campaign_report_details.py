"""The call's own record in the workbook, and the analytics it feeds in the overview.

These are the columns a standalone call-analytics export carries — who dialled whom, how
the call went, and the artefacts it produced.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from core.models.agent_call_log import AgentCallLog
from core.models.agent_call_recording import AgentCallRecording
from core.models.booking import Booking
from core.models.booking_participant import BookingParticipant
from core.services import campaign_reports as report_service
from core.tests.campaigns.test_campaign_report import Format, ReportTestCase, Status
from core.tests.scheduling import factories as f

STORAGE = "core.services.campaign_reports.AzureBlobStorageService"

DETAIL_HEADERS = [
    "Call from",
    "Call to",
    "Bot",
    "Call started at (Asia/Kolkata)",
    "Call ended at (Asia/Kolkata)",
    "Attended by",
    "Sentiment",
    "Sentiment confidence",
    "Detected language",
    "Appointment booked for (Asia/Kolkata)",
    "Follow-up note",
    "Recording",
    "AI summary",
    "Transcript",
    "Call logged at (Asia/Kolkata)",
]


class DetailTestCase(ReportTestCase):
    def build_with_storage(self, report, link="https://signed.example/recording.mp3"):
        """Like `build`, with the signing call stubbed so a link can be asserted."""
        captured = {}

        def upload(key, file_obj, content_type=None):
            captured.update(key=key, data=file_obj.read(), content_type=content_type)
            return key

        with mock.patch(STORAGE) as storage_cls:
            storage = storage_cls.return_value
            storage.upload_file.side_effect = upload
            storage.get_presigned_url_for_object.return_value = link
            report_service.build_report(str(report.pk))

        report.refresh_from_db()
        return report, captured, storage

    def headers(self, workbook):
        sheet = workbook["Members"]
        return [cell.value for cell in next(sheet.iter_rows(max_row=1))]

    def row_for(self, workbook, name):
        return next(row for row in self.member_rows(workbook) if row["Name"] == name)

    def log(self, member, provider_call_id="call-detail-1", **fields):
        payload = dict(
            tenant=self.tenant, agent=self.agent, provider_call_id=provider_call_id,
            caller_number="+18005550100", callee_number=member.phone_e164,
            direction=AgentCallLog.CallDirection.OUTBOUND, duration_seconds=95,
            campaign_member=member,
        )
        payload.update(fields)
        return AgentCallLog.objects.create(**payload)


class CallDetailColumnTests(DetailTestCase):
    def test_the_call_columns_follow_the_fixed_ones(self):
        _report, captured, _storage = self.build_with_storage(self.report())

        headers = self.headers(self.workbook(captured))
        start = headers.index("Probing questions asked") + 1
        self.assertEqual(headers[start:], DETAIL_HEADERS)

    def test_the_call_record_fills_its_columns(self):
        self.log(
            self.completed,
            started_at=self.called_at,
            ended_at=self.called_at + timedelta(seconds=95),
            mood="Positive",
            mood_confidence=0.87,
            preferred_language="Hindi",
            ai_summary="Asked for a callback next week.",
            follow_up_detail="Call again after Diwali.",
        )

        _report, captured, _storage = self.build_with_storage(self.report())

        row = self.row_for(self.workbook(captured), "Ada Lovelace")
        self.assertEqual(row["Call from"], "+18005550100")
        self.assertEqual(row["Call to"], "+14155552671")
        self.assertEqual(row["Bot"], "Sales Bot")
        # 03:58 UTC is 09:28 in the campaign's zone.
        self.assertEqual(row["Call started at (Asia/Kolkata)"], datetime(2026, 9, 4, 9, 28))
        self.assertEqual(row["Call ended at (Asia/Kolkata)"], datetime(2026, 9, 4, 9, 29, 35))
        self.assertEqual(row["Attended by"], "AI")
        self.assertEqual(row["Sentiment"], "Positive")
        self.assertEqual(row["Sentiment confidence"], 0.87)
        self.assertEqual(row["Detected language"], "Hindi")
        self.assertEqual(row["AI summary"], "Asked for a callback next week.")
        self.assertEqual(row["Follow-up note"], "Call again after Diwali.")

    def test_a_transferred_call_reads_human(self):
        """Nothing records who finished a call, so the transfer note is the signal."""
        self.log(self.completed, human_transfer_detail="Asked for a person at 00:42.")

        _report, captured, _storage = self.build_with_storage(self.report())

        self.assertEqual(
            self.row_for(self.workbook(captured), "Ada Lovelace")["Attended by"], "Human"
        )

    def test_a_member_never_called_has_blank_call_cells(self):
        """Blank, not "AI": nobody attended a call that never happened."""
        _report, captured, _storage = self.build_with_storage(self.report())

        row = self.row_for(self.workbook(captured), "Grace Hopper")
        for header in DETAIL_HEADERS:
            self.assertIsNone(row[header], header)

    def test_the_transcript_reads_as_speaker_lines(self):
        self.log(
            self.completed,
            transcript=[
                {"id": "t-0", "speaker": "bot", "text": "Good morning."},
                {"id": "t-1", "speaker": "customer", "text": "Not interested."},
            ],
        )

        _report, captured, _storage = self.build_with_storage(self.report())

        self.assertEqual(
            self.row_for(self.workbook(captured), "Ada Lovelace")["Transcript"],
            "bot: Good morning.\ncustomer: Not interested.",
        )

    def test_a_long_transcript_is_cut_to_what_a_cell_holds(self):
        """Excel refuses a cell past 32,767 characters — the tail goes, not the report."""
        self.log(
            self.completed,
            transcript=[
                {"speaker": "bot", "text": f"Line {index} of a very long call."}
                for index in range(2000)
            ],
        )

        report, captured, _storage = self.build_with_storage(self.report())

        self.assertEqual(report.status, Status.COMPLETED)
        transcript = self.row_for(self.workbook(captured), "Ada Lovelace")["Transcript"]
        self.assertLessEqual(len(transcript), report_service.TRANSCRIPT_LIMIT)
        self.assertTrue(transcript.endswith("…"))

    def test_a_transcript_of_the_wrong_shape_is_simply_empty(self):
        self.log(self.completed, transcript={"turns": "not a list"})

        report, captured, _storage = self.build_with_storage(self.report())

        self.assertEqual(report.status, Status.COMPLETED)
        self.assertIsNone(
            self.row_for(self.workbook(captured), "Ada Lovelace")["Transcript"]
        )


class RecordingLinkTests(DetailTestCase):
    def test_the_runners_own_url_is_used_as_sent(self):
        self.log(self.completed, external_recording_url="https://runner.example/r/1.mp3")

        _report, captured, storage = self.build_with_storage(self.report())

        self.assertEqual(
            self.row_for(self.workbook(captured), "Ada Lovelace")["Recording"],
            "https://runner.example/r/1.mp3",
        )
        storage.get_presigned_url_for_object.assert_not_called()

    def test_our_own_copy_is_signed_for_the_life_of_the_report(self):
        """A link that expires before the workbook does reads as a broken report."""
        recording = AgentCallRecording.objects.create(
            tenant=self.tenant, file="call-recordings/ada.mp3", original_name="ada.mp3"
        )
        self.log(self.completed, recording_file=recording)

        _report, captured, storage = self.build_with_storage(self.report())

        self.assertEqual(
            self.row_for(self.workbook(captured), "Ada Lovelace")["Recording"],
            "https://signed.example/recording.mp3",
        )
        storage.get_presigned_url_for_object.assert_called_with(
            "call-recordings/ada.mp3", expiry_hours=report_service.recording_link_hours()
        )

    def test_a_call_with_no_recording_leaves_the_cell_blank(self):
        self.log(self.completed)

        _report, captured, storage = self.build_with_storage(self.report())

        self.assertIsNone(
            self.row_for(self.workbook(captured), "Ada Lovelace")["Recording"]
        )
        storage.get_presigned_url_for_object.assert_not_called()


class AppointmentTests(DetailTestCase):
    def book(self, contact, start_time):
        template = f.make_template(self.tenant)
        booking = Booking.objects.create(
            tenant=self.tenant, event_template=template, title="Product demo",
            primary_host=self.user, start_time=start_time,
            end_time=start_time + timedelta(minutes=30),
            effective_start_time=start_time,
            effective_end_time=start_time + timedelta(minutes=30),
            status=Booking.Status.SCHEDULED,
        )
        BookingParticipant.objects.create(
            tenant=self.tenant, booking=booking, name=contact.full_name,
            email="ada@example.com", contact=contact,
            role=BookingParticipant.Role.ATTENDEE,
        )
        return booking

    def test_an_appointment_booked_since_the_campaign_started_appears_on_the_row(self):
        self.book(self.completed.contact, datetime(2026, 9, 10, 4, 30, tzinfo=dt_timezone.utc))

        _report, captured, _storage = self.build_with_storage(self.report())

        self.assertEqual(
            self.row_for(self.workbook(captured), "Ada Lovelace")[
                "Appointment booked for (Asia/Kolkata)"
            ],
            datetime(2026, 9, 10, 10, 0),
        )

    def test_people_without_an_appointment_keep_a_blank_cell(self):
        self.book(self.completed.contact, datetime(2026, 9, 10, 4, 30, tzinfo=dt_timezone.utc))

        _report, captured, _storage = self.build_with_storage(self.report())

        self.assertIsNone(
            self.row_for(self.workbook(captured), "Grace Hopper")[
                "Appointment booked for (Asia/Kolkata)"
            ]
        )

    def test_the_overview_counts_people_with_appointments(self):
        self.book(self.completed.contact, datetime(2026, 9, 10, 4, 30, tzinfo=dt_timezone.utc))

        _report, captured, _storage = self.build_with_storage(self.report())

        self.assertEqual(self.overview(self.workbook(captured))["Appointments booked"], 1)


class OverviewAnalyticsTests(DetailTestCase):
    def setUp(self):
        super().setUp()
        # A spread of calls: one short unanswered, one mid, one long, plus a transfer.
        self.log(self.failed, "call-analytics-1", duration_seconds=0, outcome="no answer")
        self.log(
            self.completed, "call-analytics-2", duration_seconds=45,
            mood="Neutral", outcome="information provided",
        )
        self.log(
            self.suppressed, "call-analytics-3", duration_seconds=150,
            mood="Negative", outcome="information provided",
            human_transfer_detail="Wanted a human.",
        )

    def overview_of(self, report=None):
        _report, captured, _storage = self.build_with_storage(report or self.report())
        return self.overview(self.workbook(captured))

    def test_calls_that_never_connected_are_counted_and_rated(self):
        overview = self.overview_of()

        self.assertEqual(overview["Not picked up"], 1)
        self.assertEqual(overview["Not picked up rate"], "25.0%")

    def test_the_longest_call_is_reported(self):
        self.assertEqual(self.overview_of()["Longest call"], "2m 30s")

    def test_attempts_are_totalled_from_the_members(self):
        # The fixture's members report 1 and 3 attempts.
        self.assertEqual(self.overview_of()["Attempts reported"], 4)

    def test_calls_are_bucketed_by_length(self):
        overview = self.overview_of()

        self.assertEqual(overview["Under 30 seconds"], 0)
        self.assertEqual(overview["30 to 60 seconds"], 1)
        self.assertEqual(overview["60 to 120 seconds"], 1)
        self.assertEqual(overview["Over 120 seconds"], 1)

    def test_transfers_split_from_bot_handled_calls(self):
        overview = self.overview_of()

        self.assertEqual(overview["Transferred to a human"], 1)
        self.assertEqual(overview["Handled by the bot"], 2)

    def test_sentiment_is_broken_down(self):
        overview = self.overview_of()

        self.assertEqual(overview["Neutral"], 1)
        self.assertEqual(overview["Negative"], 1)

    def test_dispositions_are_counted_as_the_runner_sent_them(self):
        overview = self.overview_of()

        self.assertEqual(overview["information provided"], 2)
        self.assertEqual(overview["no answer"], 1)

    def test_the_direction_is_stated(self):
        self.assertEqual(self.overview_of()["Direction"], "Outbound")


class FormatDifferenceTests(DetailTestCase):
    def test_the_pdf_keeps_the_fixed_columns_only(self):
        """Fifteen more columns would make its landscape table unreadable, not richer."""
        self.assertEqual(len(report_service.member_columns("UTC")), 14)
        self.assertEqual(
            len(report_service.member_columns("UTC", detailed=True)), 14 + len(DETAIL_HEADERS)
        )

    def test_a_pdf_still_builds_with_call_data_present(self):
        self.log(self.completed, ai_summary="Long summary.", mood="Positive")

        report, captured, _storage = self.build_with_storage(self.report(Format.PDF))

        self.assertEqual(report.status, Status.COMPLETED)
        self.assertTrue(captured["data"].startswith(b"%PDF"))
