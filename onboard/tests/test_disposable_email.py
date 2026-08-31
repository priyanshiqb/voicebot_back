"""Disposable-email rejection on the two onboarding paths that accept an address.

A ``payment.authorized`` for a paid plan arrived against a ``@yopmail.com`` address,
which is what these exist to stop. The rule lives in ``utils.emails.disposable`` and
is applied by ``TempUserSignupSerializer.validate_email`` and
``ChangeEmailSerializer.validate_new_email`` — both, because otherwise the change-email
step is a way to walk a registration onto a throwaway address after signup has passed.
"""

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework import serializers

from onboard.serializers.onboarding.email import ChangeEmailSerializer
from onboard.serializers.onboarding.signup import TempUserSignupSerializer
from utils.emails import disposable_domain_for, is_disposable_email


class DisposableDetectionTests(SimpleTestCase):
    """The rule itself, with no serializer and no database in the way."""

    def test_known_throwaway_providers_are_recognised(self):
        for address in (
            "kamal.hasan@yopmail.com",       # the one that actually got through
            "someone@mailinator.com",
            "someone@10minutemail.com",
        ):
            with self.subTest(address=address):
                self.assertTrue(is_disposable_email(address))

    def test_real_mailboxes_are_left_alone(self):
        """The check must not cost a real signup. Gmail is not disposable."""
        for address in (
            "founder@gmail.com",
            "owner@outlook.com",
            "ops@acmecorp.com",
            "first.last+tag@somebusiness.co.in",
        ):
            with self.subTest(address=address):
                self.assertFalse(is_disposable_email(address))

    def test_a_subdomain_of_a_throwaway_domain_is_also_throwaway(self):
        """Otherwise the check is avoided by typing one extra label."""
        self.assertEqual(
            disposable_domain_for("someone@mail.yopmail.com"), "yopmail.com"
        )

    def test_the_matched_domain_is_reported_not_just_a_boolean(self):
        """With ~8k entries, "why was this rejected" is otherwise unanswerable."""
        self.assertEqual(disposable_domain_for("x@yopmail.com"), "yopmail.com")
        self.assertIsNone(disposable_domain_for("x@acmecorp.com"))

    def test_case_and_whitespace_do_not_evade_the_check(self):
        self.assertTrue(is_disposable_email("  Someone@YOPMail.Com  "))

    def test_a_trailing_dot_does_not_evade_the_check(self):
        """`user@yopmail.com.` is the same host; the root label is dropped."""
        self.assertTrue(is_disposable_email("someone@yopmail.com."))

    def test_malformed_input_is_not_an_error(self):
        """Format is EmailField's job. This must not raise on the way past."""
        for value in ("", None, "not-an-email", "@", "trailing@"):
            with self.subTest(value=value):
                self.assertFalse(is_disposable_email(value))

    def test_a_bare_tld_is_never_tested(self):
        """The upward walk stops at two labels rather than asking about `com`."""
        self.assertIsNone(disposable_domain_for("someone@com"))

    @override_settings(DISPOSABLE_EMAIL_ALLOWLIST=["yopmail.com"])
    def test_the_allowlist_overrides_the_packaged_list(self):
        """The escape hatch. The upstream list occasionally catches a real customer."""
        self.assertFalse(is_disposable_email("someone@yopmail.com"))

    @override_settings(DISPOSABLE_EMAIL_ALLOWLIST=["ok.yopmail.com"])
    def test_the_allowlist_can_free_one_subdomain_of_a_blocked_parent(self):
        """Allowlist is consulted at each level before the blocklist, so the more
        specific entry wins over the blocked parent."""
        self.assertFalse(is_disposable_email("someone@ok.yopmail.com"))
        self.assertTrue(is_disposable_email("someone@other.yopmail.com"))

    @override_settings(DISPOSABLE_EMAIL_EXTRA_BLOCKED=["brand-new-throwaway.test"])
    def test_extra_blocked_covers_what_the_pinned_list_has_not_caught_up_with(self):
        self.assertTrue(is_disposable_email("someone@brand-new-throwaway.test"))

    @override_settings(DISPOSABLE_EMAIL_EXTRA_BLOCKED=["@Spaced.Test "])
    def test_configured_domains_tolerate_stray_at_signs_and_spacing(self):
        self.assertTrue(is_disposable_email("someone@spaced.test"))


class SignupRejectionTests(TestCase):
    """SU: the serializer field that the signup endpoint runs."""

    def test_signup_rejects_a_disposable_address(self):
        with self.assertRaises(serializers.ValidationError) as caught:
            TempUserSignupSerializer().validate_email("kamal.hasan@yopmail.com")
        self.assertIn("Disposable", str(caught.exception))

    def test_signup_still_accepts_an_ordinary_address(self):
        self.assertEqual(
            TempUserSignupSerializer().validate_email("  Founder@AcmeCorp.com "),
            "founder@acmecorp.com",
        )


class ChangeEmailRejectionTests(TestCase):
    """CE: the second door. Passing signup then switching to a throwaway address
    would defeat the rule entirely."""

    def test_change_email_rejects_a_disposable_address(self):
        with self.assertRaises(serializers.ValidationError) as caught:
            ChangeEmailSerializer().validate_new_email("someone@mailinator.com")
        self.assertIn("Disposable", str(caught.exception))

    def test_change_email_still_accepts_an_ordinary_address(self):
        self.assertEqual(
            ChangeEmailSerializer().validate_new_email("Ops@AcmeCorp.com"),
            "ops@acmecorp.com",
        )
