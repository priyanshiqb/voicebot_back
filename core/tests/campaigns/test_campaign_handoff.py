"""
Pushing a campaign to the voice runner.

The invariant that carries the weight here is that `handoff_status` only ever moves
forward. The runner can pull /context/ and set ACKED before the notify task has
committed SENT, so an unguarded write would drag an acknowledged campaign backwards and
leave it permanently reading as never-acknowledged. Most of these exercise that race and
the failure paths around it rather than the happy path.

No test may make a real outbound call: `requests.post` is patched everywhere.
"""

from datetime import timedelta
from unittest import mock

import requests
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from core.models.agent import Agent
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember
from core.models.contact import Contact
from core.services import campaign_handoff as handoff_service
from core.services import campaigns as campaign_service
from core.tasks import notify_runner_campaign_start, retry_stale_handoffs
from tenant.models.Tenant import Tenant
from user.models import CustomUser as User

NOTIFY_URL = "http://runner.test/api/v1/campaigns/start/"

HANDOFF_SETTINGS = dict(
    CAMPAIGN_RUNNER_NOTIFY_ENABLED=True,
    CAMPAIGN_RUNNER_NOTIFY_URL=NOTIFY_URL,
    CAMPAIGN_RUNNER_TOKEN="test-runner-token",
    CAMPAIGN_RUNNER_TIMEOUT=10,
    CAMPAIGN_RUNNER_STALE_HANDOFF_MINUTES=20,
)


def _response(status_code, text="", headers=None):
    response = mock.Mock()
    response.status_code = status_code
    response.text = text
    response.headers = headers or {}
    return response


class HandoffTestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-handoff")
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Bot", persona_name="P"
        )
        self.campaign = Campaign.objects.create(
            tenant=self.tenant,
            agent=self.agent,
            name="Handoff",
            status=Campaign.Status.MATERIALIZING,
            handoff_id=campaign_service.new_handoff_id(),
        )

    def _contact(self, phone, **kwargs):
        contact = Contact.objects.create(
            tenant=self.tenant, phone_number=phone, **kwargs
        )
        self.campaign.contacts.add(contact)
        return contact

    def _handing_off(self, started_at=None):
        """Put the campaign where the notify task expects to find it."""
        self.campaign.status = Campaign.Status.HANDING_OFF
        self.campaign.handoff_status = Campaign.HandoffStatus.PENDING
        self.campaign.started_at = started_at or timezone.now()
        self.campaign.pending_count = 2
        self.campaign.save()
        return self.campaign

    def _refreshed(self):
        self.campaign.refresh_from_db()
        return self.campaign


# ----------------------------------------------------------------------------
# Materialisation branch
# ----------------------------------------------------------------------------


class MaterializeHandoffTests(HandoffTestCase):
    @override_settings(**HANDOFF_SETTINGS)
    def test_materialize_ends_in_handing_off_and_enqueues(self):
        self._contact("+14155552671")

        # The enqueue is an on_commit hook, which never fires inside TestCase's own
        # transaction unless the callbacks are captured and run explicitly.
        with mock.patch("core.tasks.notify_runner_campaign_start.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                campaign = campaign_service.materialize_campaign(self.campaign.pk)

        self.assertEqual(campaign.status, Campaign.Status.HANDING_OFF)
        self.assertEqual(campaign.handoff_status, Campaign.HandoffStatus.PENDING)
        self.assertIsNotNone(campaign.started_at)
        delay.assert_called_once_with(str(self.campaign.pk))

    @override_settings(CAMPAIGN_RUNNER_NOTIFY_ENABLED=False)
    def test_flag_off_keeps_the_old_behaviour_exactly(self):
        """The regression guard for every campaign test that predates this feature."""
        self._contact("+14155552671")

        with mock.patch("core.tasks.notify_runner_campaign_start.delay") as delay:
            campaign = campaign_service.materialize_campaign(self.campaign.pk)

        self.assertEqual(campaign.status, Campaign.Status.RUNNING)
        delay.assert_not_called()

    @override_settings(**HANDOFF_SETTINGS)
    def test_fully_suppressed_campaign_is_never_handed_off(self):
        """Nobody to call means nobody to tell — the existing guard must still win."""
        self._contact("+14155552671", do_not_call=True)

        with mock.patch("core.tasks.notify_runner_campaign_start.delay") as delay:
            campaign = campaign_service.materialize_campaign(self.campaign.pk)

        self.assertEqual(campaign.status, Campaign.Status.FAILED)
        delay.assert_not_called()


# ----------------------------------------------------------------------------
# The outbound call
# ----------------------------------------------------------------------------


@override_settings(**HANDOFF_SETTINGS)
class NotifyRunnerTests(HandoffTestCase):
    def test_posts_identity_payload_and_bearer_token(self):
        campaign = self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(202)
            handoff_service.notify_runner(campaign)

        url, kwargs = post.call_args[0][0], post.call_args[1]
        self.assertEqual(url, NOTIFY_URL)
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(
            kwargs["json"],
            {
                "campaign_id": str(campaign.pk),
                "tenant_id": str(self.tenant.id),
                "handoff_id": str(campaign.handoff_id),
                "agent_id": str(self.agent.id),
                "pending_count": 2,
            },
        )
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer test-runner-token"
        )
        self.assertEqual(
            kwargs["headers"]["X-Handoff-Id"], str(campaign.handoff_id)
        )

    def test_audience_is_never_pushed(self):
        """The runner pulls members from /context/; the push carries ids only."""
        campaign = self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(200)
            handoff_service.notify_runner(campaign)

        self.assertNotIn("members", post.call_args[1]["json"])

    @override_settings(CAMPAIGN_RUNNER_NOTIFY_URL="")
    def test_missing_url_is_permanent_not_transient(self):
        campaign = self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            with self.assertRaises(handoff_service.PermanentHandoffError):
                handoff_service.notify_runner(campaign)

        post.assert_not_called()

    def test_timeout_is_transient(self):
        campaign = self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.side_effect = requests.Timeout("timed out")
            with self.assertRaises(handoff_service.TransientHandoffError):
                handoff_service.notify_runner(campaign)

    def test_server_error_is_transient(self):
        campaign = self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(503, "upstream down")
            with self.assertRaises(handoff_service.TransientHandoffError):
                handoff_service.notify_runner(campaign)

    def test_client_error_is_permanent(self):
        campaign = self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(401, "bad token")
            with self.assertRaises(handoff_service.PermanentHandoffError):
                handoff_service.notify_runner(campaign)

    def test_rate_limit_is_transient_and_honours_retry_after(self):
        campaign = self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(429, "slow down", {"Retry-After": "45"})
            with self.assertRaises(handoff_service.TransientHandoffError) as ctx:
                handoff_service.notify_runner(campaign)

        self.assertEqual(ctx.exception.retry_after, 45)

    def test_unparseable_retry_after_falls_back_to_our_own_backoff(self):
        campaign = self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(
                429, "slow down", {"Retry-After": "Wed, 01 Jan 2031 00:00:00 GMT"}
            )
            with self.assertRaises(handoff_service.TransientHandoffError) as ctx:
                handoff_service.notify_runner(campaign)

        self.assertIsNone(ctx.exception.retry_after)


# ----------------------------------------------------------------------------
# The ACK race — the reason mark_sent uses guarded updates
# ----------------------------------------------------------------------------


@override_settings(**HANDOFF_SETTINGS)
class MarkSentGuardTests(HandoffTestCase):
    def test_advances_pending_to_sent_and_running(self):
        self._handing_off()

        self.assertTrue(handoff_service.mark_sent(self.campaign.pk))

        campaign = self._refreshed()
        self.assertEqual(campaign.handoff_status, Campaign.HandoffStatus.SENT)
        self.assertEqual(campaign.status, Campaign.Status.RUNNING)

    def test_does_not_drag_an_acked_campaign_back_to_sent(self):
        """The runner pulled /context/ before this task committed. ACKED must survive."""
        self._handing_off()
        Campaign.objects.filter(pk=self.campaign.pk).update(
            handoff_status=Campaign.HandoffStatus.ACKED
        )

        handoff_service.mark_sent(self.campaign.pk)

        campaign = self._refreshed()
        self.assertEqual(campaign.handoff_status, Campaign.HandoffStatus.ACKED)
        # The campaign still starts running — only the handoff_status is protected.
        self.assertEqual(campaign.status, Campaign.Status.RUNNING)

    def test_does_not_resurrect_a_cancelled_campaign(self):
        """Cancel landed mid-handoff. The notify must not undo it."""
        self._handing_off()
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.Status.CANCELLED
        )

        self.assertFalse(handoff_service.mark_sent(self.campaign.pk))
        self.assertEqual(self._refreshed().status, Campaign.Status.CANCELLED)

    def test_updated_at_is_maintained_by_hand(self):
        """auto_now does not fire on queryset .update(), so mark_sent sets it."""
        self._handing_off()
        Campaign.objects.filter(pk=self.campaign.pk).update(
            updated_at=timezone.now() - timedelta(hours=1)
        )
        before = self._refreshed().updated_at

        handoff_service.mark_sent(self.campaign.pk)

        self.assertGreater(self._refreshed().updated_at, before)


@override_settings(**HANDOFF_SETTINGS)
class NotifyCampaignStartTests(HandoffTestCase):
    def test_skips_a_campaign_that_moved_on(self):
        self._handing_off()
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.Status.CANCELLED
        )

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            self.assertIsNone(
                handoff_service.notify_campaign_start(self.campaign.pk)
            )

        post.assert_not_called()

    def test_skips_a_campaign_the_runner_already_acked(self):
        """It plainly knows. Re-sending is a duplicate it has to discard."""
        self._handing_off()
        Campaign.objects.filter(pk=self.campaign.pk).update(
            handoff_status=Campaign.HandoffStatus.ACKED
        )

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            self.assertIsNone(
                handoff_service.notify_campaign_start(self.campaign.pk)
            )

        post.assert_not_called()

    def test_missing_campaign_returns_none_rather_than_raising(self):
        import uuid

        with mock.patch("core.services.campaign_handoff.requests.post"):
            self.assertIsNone(handoff_service.notify_campaign_start(uuid.uuid4()))


# ----------------------------------------------------------------------------
# Failure recording
# ----------------------------------------------------------------------------


@override_settings(**HANDOFF_SETTINGS)
class HandoffFailureTests(HandoffTestCase):
    def test_permanent_failure_records_without_killing_the_campaign(self):
        self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(400, "malformed")
            notify_runner_campaign_start(str(self.campaign.pk))

        campaign = self._refreshed()
        self.assertEqual(campaign.handoff_status, Campaign.HandoffStatus.FAILED)
        # Still HANDING_OFF, not FAILED: the audience is materialised and correct, and
        # HANDING_OFF is workable, so a manual replay can still pull /context/.
        self.assertEqual(campaign.status, Campaign.Status.HANDING_OFF)
        self.assertIn("400", campaign.error_message)

    def test_permanent_failure_does_not_retry(self):
        self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(403, "forbidden")
            with mock.patch.object(notify_runner_campaign_start, "retry") as retry:
                notify_runner_campaign_start(str(self.campaign.pk))

        retry.assert_not_called()
        self.assertEqual(post.call_count, 1)

    def test_transient_failure_retries_and_leaves_status_alone(self):
        self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(500, "boom")
            with mock.patch.object(
                notify_runner_campaign_start, "retry", side_effect=RuntimeError("retried")
            ) as retry:
                with self.assertRaises(RuntimeError):
                    notify_runner_campaign_start(str(self.campaign.pk))

        retry.assert_called_once()
        campaign = self._refreshed()
        self.assertEqual(campaign.handoff_status, Campaign.HandoffStatus.PENDING)
        self.assertEqual(campaign.status, Campaign.Status.HANDING_OFF)

    def test_exhausted_retries_are_recorded_before_celery_gives_up(self):
        """Otherwise the campaign reads PENDING forever with nothing to say why."""
        self._handing_off()

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(502, "bad gateway")
            # push_request, not mock.patch: celery's `request` is a property with no
            # deleter, so mock cannot restore it afterwards.
            notify_runner_campaign_start.push_request(
                retries=notify_runner_campaign_start.max_retries
            )
            try:
                notify_runner_campaign_start(str(self.campaign.pk))
            finally:
                notify_runner_campaign_start.pop_request()

        campaign = self._refreshed()
        self.assertEqual(campaign.handoff_status, Campaign.HandoffStatus.FAILED)
        self.assertEqual(campaign.status, Campaign.Status.HANDING_OFF)
        self.assertIn("Gave up", campaign.error_message)


# ----------------------------------------------------------------------------
# The sweeper
# ----------------------------------------------------------------------------


@override_settings(**HANDOFF_SETTINGS)
class StaleHandoffSweeperTests(HandoffTestCase):
    def _stale(self, minutes=30):
        return self._handing_off(
            started_at=timezone.now() - timedelta(minutes=minutes)
        )

    def test_picks_up_a_stalled_pending_handoff(self):
        self._stale()
        self.assertIn(self.campaign.pk, handoff_service.stale_handoffs())

    def test_picks_up_a_failed_handoff_for_another_try(self):
        self._stale()
        Campaign.objects.filter(pk=self.campaign.pk).update(
            handoff_status=Campaign.HandoffStatus.FAILED
        )
        self.assertIn(self.campaign.pk, handoff_service.stale_handoffs())

    def test_ignores_a_handoff_still_inside_the_retry_budget(self):
        """Below the floor the notify task is still retrying; do not stack on it."""
        self._stale(minutes=5)
        self.assertNotIn(self.campaign.pk, handoff_service.stale_handoffs())

    def test_ignores_an_acknowledged_campaign(self):
        self._stale()
        Campaign.objects.filter(pk=self.campaign.pk).update(
            handoff_status=Campaign.HandoffStatus.ACKED
        )
        self.assertNotIn(self.campaign.pk, handoff_service.stale_handoffs())

    def test_ignores_a_campaign_that_is_no_longer_handing_off(self):
        self._stale()
        Campaign.objects.filter(pk=self.campaign.pk).update(
            status=Campaign.Status.CANCELLED
        )
        self.assertNotIn(self.campaign.pk, handoff_service.stale_handoffs())

    def test_resend_reuses_the_same_handoff_id(self):
        """A re-notify must be a duplicate the runner can discard, not new work."""
        campaign = self._stale()
        original = campaign.handoff_id

        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(202)
            notify_runner_campaign_start(str(campaign.pk))

        self.assertEqual(
            post.call_args[1]["json"]["handoff_id"], str(original)
        )
        self.assertEqual(self._refreshed().handoff_id, original)

    def test_task_enqueues_one_notify_per_stalled_campaign(self):
        self._stale()

        with mock.patch("core.tasks.notify_runner_campaign_start.delay") as delay:
            count = retry_stale_handoffs()

        self.assertEqual(count, 1)
        delay.assert_called_once_with(str(self.campaign.pk))


# ----------------------------------------------------------------------------
# End to end, against the real runner endpoint
# ----------------------------------------------------------------------------


@override_settings(**HANDOFF_SETTINGS)
class HandoffAcknowledgementTests(HandoffTestCase):
    """The two halves joined up: our push, then the runner's pull."""

    BASE = "/api/v1/core/campaign-runner/"

    def setUp(self):
        super().setUp()
        self.runner_user = User.objects.create_user(
            username="runner-handoff",
            email="runner-handoff@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT,
        )
        self.runner_client = APIClient()
        self.runner_client.force_authenticate(user=self.runner_user)
        # Mandatory on every agent-token endpoint; without it this is a 400 regardless
        # of what is being tested.
        self.runner_client.credentials(HTTP_X_TENANT_ID=str(self.tenant.id))

    def _member(self, phone="+14155552671"):
        return CampaignMember.objects.create(
            tenant=self.tenant,
            campaign=self.campaign,
            contact=self._contact(phone),
            phone_e164=phone,
            status=CampaignMember.Status.PENDING,
        )

    def _notify(self):
        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            post.return_value = _response(202)
            notify_runner_campaign_start(str(self.campaign.pk))

    def test_full_sequence_pending_sent_acked(self):
        self._handing_off()
        self._member()
        self.assertEqual(
            self._refreshed().handoff_status, Campaign.HandoffStatus.PENDING
        )

        self._notify()
        self.assertEqual(
            self._refreshed().handoff_status, Campaign.HandoffStatus.SENT
        )

        response = self.runner_client.get(f"{self.BASE}{self.campaign.pk}/context/")
        self.assertEqual(response.status_code, 200)

        campaign = self._refreshed()
        self.assertEqual(campaign.handoff_status, Campaign.HandoffStatus.ACKED)
        self.assertIsNotNone(campaign.handed_off_at)

    def test_runner_may_pull_while_still_handing_off(self):
        """HANDING_OFF is in WORKABLE_STATUSES, so an eager runner is served."""
        self._handing_off()
        self._member()

        response = self.runner_client.get(f"{self.BASE}{self.campaign.pk}/context/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self._refreshed().handoff_status, Campaign.HandoffStatus.ACKED
        )

    def test_an_early_ack_survives_the_notify_that_follows_it(self):
        """The real race, end to end: the pull lands before the task commits SENT."""
        self._handing_off()
        self._member()

        self.runner_client.get(f"{self.BASE}{self.campaign.pk}/context/")
        self.assertEqual(
            self._refreshed().handoff_status, Campaign.HandoffStatus.ACKED
        )

        # notify_campaign_start sees ACKED and declines to send at all.
        with mock.patch("core.services.campaign_handoff.requests.post") as post:
            notify_runner_campaign_start(str(self.campaign.pk))
        post.assert_not_called()

        self.assertEqual(
            self._refreshed().handoff_status, Campaign.HandoffStatus.ACKED
        )
