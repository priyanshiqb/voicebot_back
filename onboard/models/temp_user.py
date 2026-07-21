from django.db import models
from utils.models.base_model_core import BaseModelCore
from django.utils import timezone


class TempUser(BaseModelCore):
    """
    TempUser Model representing temporary/pending registration accounts.
    It separates unverified users from the main CustomUser authentication database.
    """
    email = models.EmailField(
        help_text="Primary email address of the pending registrant."
    )
    phone_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Phone number of the pending registrant."
    )
    referral_code = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Referral code used during signup."
    )
    password_hash = models.CharField(
        max_length=255,
        help_text="Encrypted password representation."
    )
    first_name = models.CharField(
        max_length=100,
        help_text="First name of the pending registrant."
    )
    last_name = models.CharField(
        max_length=100,
        help_text="Last name of the pending registrant."
    )
    business_name = models.CharField(
        max_length=255,
        help_text="Registered business/company name."
    )
    otp_code = models.CharField(
        max_length=6,
        blank=True,
        null=True,
        help_text="6-digit numeric verification OTP."
    )
    otp_created_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp of OTP generation for expiration checks."
    )
    is_email_verified = models.BooleanField(
        default=False,
        help_text="Flag indicating whether the registrant has successfully verified their OTP."
    )
    is_onboarded = models.BooleanField(
        default=False,
        help_text="Flag indicating whether the registrant has completed onboarding."
    )
    selected_plan = models.ForeignKey(
        "onboard.Plan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pending_registrations",
        help_text="The subscription plan selected during signup."
    )
    timezone = models.ForeignKey(
        "onboard.Timezone",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="temp_users",
        help_text="Selected timezone captured during onboarding."
    )
    language = models.ForeignKey(
        "onboard.Language",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="temp_users",
        help_text="Selected language captured during onboarding."
    )

    class Meta:
        db_table = "onboard_temp_user"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(is_deleted=False),
                name="unique_active_temp_user_email"
            )
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"

    def save(self, *args, **kwargs):
        if not self.timezone_id:
            from onboard.models.timezone import get_default_timezone
            self.timezone_id = get_default_timezone()

        if not self.language_id:
            from onboard.models.language import get_default_language
            self.language_id = get_default_language()

        super().save(*args, **kwargs)

    def is_otp_expired(self):
        """
        Check if the generated OTP code is older than the configured validity in minutes.
        """
        if not self.otp_created_at:
            return True
        from django.conf import settings
        expiry_limit = timezone.now() - timezone.timedelta(minutes=settings.OTP_VALIDITY_MINUTES)
        return self.otp_created_at < expiry_limit

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    @property
    def is_superuser(self):
        return False

    @property
    def is_staff(self):
        return False

