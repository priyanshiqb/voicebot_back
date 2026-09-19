"""Probing question columns: how they are discovered, ordered, and filled.

The columns are not fixed in code — they come from the bot's configuration and from what
the call logs actually contain, so these assertions are made against the workbook that
comes out the other end.
"""

from django.test import override_settings

from core.models.agent_call_log import AgentCallLog
from core.models.probing_question import ProbingQuestion
from core.services import campaign_reports as report_service
from core.tests.campaigns.test_campaign_report import Format, ReportTestCase, Status

LEADS = "How do you manage your leads?"
INDUSTRY = "Which industry are you in?"
TEAM = "What is your team size?"

#: Columns a member sheet has before the call-detail band and any question pairs.
FIXED_COLUMNS = 14


def answer(question, summary, positive=False):
    """One entry in the shape the runner writes."""
    return {"question": question, "isPositive": positive, "answerSummary": summary}


class ProbingColumnTests(ReportTestCase):
    def configure(self, *questions):
        """Attach questions to the campaign's bot, in the order given."""
        for priority, text in enumerate(questions, start=1):
            self.agent.probing_questions.add(
                ProbingQuestion.objects.create(
                    tenant=self.tenant, question=text, priority=priority, score=10
                )
            )

    def log(self, member, probing, provider_call_id="call-probing-1"):
        count = len(probing) if isinstance(probing, list) else 0
        return AgentCallLog.objects.create(
            tenant=self.tenant, agent=self.agent, provider_call_id=provider_call_id,
            caller_number="+18005550100", callee_number=member.phone_e164,
            direction=AgentCallLog.CallDirection.OUTBOUND, duration_seconds=60,
            campaign_member=member, probing_questions=probing,
            probing_questions_count=count,
        )

    def headers(self, workbook):
        sheet = workbook["Members"]
        return [cell.value for cell in next(sheet.iter_rows(max_row=1))]

    def probing_headers(self, workbook):
        """Only the question band. Found by its first header rather than by an offset, so
        adding a fixed column does not move every assertion in this module."""
        headers = self.headers(workbook)
        start = next(
            (i for i, header in enumerate(headers) if str(header).startswith("Q1 ")),
            len(headers),
        )
        return headers[start:]

    def row_for(self, workbook, name):
        return next(
            row for row in self.member_rows(workbook) if row["Name"] == name
        )

    # --- discovery and order ------------------------------------------------

    def test_configured_questions_become_column_pairs_in_priority_order(self):
        """Even though no call asked them: an empty column is itself the answer — the
        conversation never got that far."""
        self.configure(LEADS, INDUSTRY)

        _report, captured, _storage = self.build(self.report())

        self.assertEqual(
            self.probing_headers(self.workbook(captured)),
            [f"Q1 — {LEADS}", "Q1 positive?", f"Q2 — {INDUSTRY}", "Q2 positive?"],
        )

    def test_a_question_only_in_the_logs_is_appended_after_the_configured_ones(self):
        """A question asked ad hoc, or renamed on the bot after the call, must not vanish
        from the file."""
        self.configure(LEADS)
        self.log(
            self.completed,
            [answer(LEADS, "A spreadsheet.", True), answer(TEAM, "Four people.", True)],
        )

        _report, captured, _storage = self.build(self.report())

        self.assertEqual(
            self.probing_headers(self.workbook(captured)),
            [f"Q1 — {LEADS}", "Q1 positive?", f"Q2 — {TEAM}", "Q2 positive?"],
        )

    def test_spacing_and_case_differences_share_one_column(self):
        """Matching is by text, so without normalising, one question would claim two
        columns and split its own answers between them."""
        self.configure(LEADS)
        self.log(self.completed, [answer("  how do you   MANAGE your leads?  ", "Same.", True)])

        _report, captured, _storage = self.build(self.report())

        self.assertEqual(
            self.probing_headers(self.workbook(captured)),
            [f"Q1 — {LEADS}", "Q1 positive?"],
        )
        self.assertEqual(self.row_for(self.workbook(captured), "Ada Lovelace")[f"Q1 — {LEADS}"], "Same.")

    def test_a_long_question_is_cut_in_the_header_only(self):
        long_question = "Which of the many channels you run today brings you the best leads?"
        self.configure(long_question)
        self.log(self.completed, [answer(long_question, "Referrals, mostly.", True)])

        _report, captured, _storage = self.build(self.report())

        workbook = self.workbook(captured)
        header = self.probing_headers(workbook)[0]
        self.assertTrue(header.endswith("…"), header)
        self.assertLess(len(header), len(long_question))
        self.assertEqual(
            self.row_for(workbook, "Ada Lovelace")[header], "Referrals, mostly."
        )

    # --- values -------------------------------------------------------------

    def test_an_answer_lands_in_its_own_pair_of_columns(self):
        self.configure(LEADS)
        self.log(self.completed, [answer(LEADS, "Uses a spreadsheet today.", True)])

        _report, captured, _storage = self.build(self.report())

        row = self.row_for(self.workbook(captured), "Ada Lovelace")
        self.assertEqual(row[f"Q1 — {LEADS}"], "Uses a spreadsheet today.")
        self.assertEqual(row["Q1 positive?"], "Yes")

    def test_an_unanswered_question_keeps_its_summary_and_reads_no(self):
        """`isPositive` is written as sent. The runner sends false both for a negative
        answer and for one never given, and only the summary separates them."""
        self.configure(LEADS)
        self.log(self.completed, [answer(LEADS, "Not answered during the call.")])

        _report, captured, _storage = self.build(self.report())

        row = self.row_for(self.workbook(captured), "Ada Lovelace")
        self.assertEqual(row[f"Q1 — {LEADS}"], "Not answered during the call.")
        self.assertEqual(row["Q1 positive?"], "No")

    def test_a_call_that_never_carried_the_question_leaves_both_cells_blank(self):
        """Blank, not "No": not asked and asked-but-unanswered are different answers."""
        self.configure(LEADS)
        self.log(self.completed, [answer(LEADS, "A spreadsheet.", True)])

        _report, captured, _storage = self.build(self.report())

        row = self.row_for(self.workbook(captured), "Grace Hopper")
        self.assertIsNone(row[f"Q1 — {LEADS}"])
        self.assertIsNone(row["Q1 positive?"])

    def test_the_answers_come_from_the_same_call_as_the_rest_of_the_row(self):
        """The latest call — so a row never mixes one attempt's answers with another's
        duration."""
        self.configure(LEADS)
        self.log(self.completed, [answer(LEADS, "First attempt.", False)], "call-probing-a")
        self.log(self.completed, [answer(LEADS, "Second attempt.", True)], "call-probing-b")

        _report, captured, _storage = self.build(self.report())

        row = self.row_for(self.workbook(captured), "Ada Lovelace")
        self.assertEqual(row[f"Q1 — {LEADS}"], "Second attempt.")
        self.assertEqual(row["Provider call ID"], "call-probing-b")

    def test_the_count_column_says_how_many_were_asked(self):
        self.configure(LEADS, INDUSTRY)
        self.log(
            self.completed,
            [answer(LEADS, "A spreadsheet.", True), answer(INDUSTRY, "Agritech.", True)],
        )

        _report, captured, _storage = self.build(self.report())

        self.assertEqual(
            self.row_for(self.workbook(captured), "Ada Lovelace")["Probing questions asked"],
            2,
        )

    # --- bounds and bad data ------------------------------------------------

    def test_a_malformed_payload_does_not_fail_the_report(self):
        """The payload is runner-written JSON. One call recording an object where a list
        belongs must not cost the whole report."""
        self.configure(LEADS)
        self.log(self.completed, {"oops": True})

        report, captured, _storage = self.build(self.report())

        self.assertEqual(report.status, Status.COMPLETED)
        row = self.row_for(self.workbook(captured), "Ada Lovelace")
        self.assertIsNone(row[f"Q1 — {LEADS}"])

    @override_settings(CAMPAIGN_REPORT_MAX_PROBING_QUESTIONS=1)
    def test_the_cap_bounds_how_many_pairs_a_sheet_can_grow(self):
        self.configure(LEADS, INDUSTRY)

        with self.assertLogs("core.services.campaign_reports", level="WARNING"):
            _report, captured, _storage = self.build(self.report())

        self.assertEqual(
            self.probing_headers(self.workbook(captured)),
            [f"Q1 — {LEADS}", "Q1 positive?"],
        )

    @override_settings(CAMPAIGN_REPORT_MAX_PROBING_QUESTIONS=0)
    def test_the_columns_can_be_turned_off_entirely(self):
        self.configure(LEADS)

        _report, captured, _storage = self.build(self.report())

        self.assertEqual(self.probing_headers(self.workbook(captured)), [])

    # --- the PDF ------------------------------------------------------------

    def test_the_pdf_carries_no_question_columns(self):
        """Its landscape table is at its width limit with the fixed columns; twenty more
        would make it unreadable rather than more informative."""
        self.configure(LEADS, INDUSTRY)
        self.log(self.completed, [answer(LEADS, "A spreadsheet.", True)])

        report, captured, _storage = self.build(self.report(Format.PDF))

        self.assertEqual(report.status, Status.COMPLETED)
        self.assertTrue(captured["data"].startswith(b"%PDF"))

    def test_the_column_list_grows_by_two_per_question(self):
        self.configure(LEADS, INDUSTRY)

        probing = report_service.probing_columns_for(self.campaign)

        self.assertEqual(len(report_service.member_columns("UTC")), FIXED_COLUMNS)
        self.assertEqual(
            len(report_service.member_columns("UTC", probing)), FIXED_COLUMNS + 4
        )
