from django.contrib.auth.models import AbstractUser
from utils.models.base_model_core import BaseModelCore
from django.db import models
from django.contrib.auth.models import BaseUserManager
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.utils import timezone

username_validator = UnicodeUsernameValidator()

class UserManager(BaseUserManager):

    def create_user(self, email, password=None, username=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")

        email = self.normalize_email(email)

        if not username:
            username = email

        user = self.model(
            email=email,
            username=username,
            **extra_fields
        )
        user.set_password(password)
        user.save(using=self._db)

        return user

    def create_superuser(self, email, password=None, username=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError("Superuser must have is_staff=True")

        if extra_fields.get('is_superuser') is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self.create_user(email, password, username, **extra_fields)



class CustomUser(AbstractUser, BaseModelCore):
    class UserType(models.TextChoices):
        """What kind of account this is, which is not the same as what it may do.

        ``USER`` is everybody who works at a tenant. **There is no separate role
        within a tenant** -- permissions there come from the tenant mapping, not from
        here -- so anything wanting "the people at this business" filters on tenant
        membership rather than on this field.

        ``ETL`` and ``VB_AGENT`` are machine accounts with real gates behind them
        (``user.authentication.etl_token``, ``user.permissions``).

        ``ADMIN`` is **reserved for platform staff** and is not assigned by any code
        path today. It is deliberately not a tenant role: a tenant's own
        administrator must never be able to reach platform operations. Two booking
        notification paths used to filter on it, matched nobody, and delivered to an
        empty audience for as long as they existed -- do not point anything at this
        value until something assigns it.
        """

        ADMIN = "ADMIN", "Platform Admin"
        USER = "USER", "User"
        ETL = "ETL", "ETL"
        VB_AGENT = "VB_AGENT", "Voicebot Agent"

    user_type = models.CharField(
        max_length=10,
        choices=UserType.choices,
        default=UserType.USER,
        help_text="Role/Type classification of the user account."
    )

    username = models.CharField(
        _('username'),
        max_length=150,
        unique=True,
        blank=True,
        null=True,
        validators=[username_validator],
    )
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    otp_code = models.CharField(
        max_length=6,
        blank=True,
        null=True,
        help_text="6-digit numeric verification OTP for active user password reset."
    )
    otp_created_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp of OTP generation for active user."
    )
    timezone = models.ForeignKey(
        "onboard.Timezone",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
        help_text="Preferred user timezone for schedules and notifications."
    )
    language = models.ForeignKey(
        "onboard.Language",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
        help_text="Preferred user language."
    )
    
    session_timeout_hours = models.IntegerField(
        default=168,
        help_text="Hours of inactivity before automated logout (default 7 days)."
    )

    created_by = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_users'
    )

    updated_by = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='updated_users'
    )

    deleted_by = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='deleted_users'
    )

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = UserManager()

    def is_otp_expired(self):
        """
        Check if the generated OTP code is older than the configured validity in minutes.
        """
        if not self.otp_created_at:
            return True
        from django.conf import settings
        expiry_limit = timezone.now() - timezone.timedelta(minutes=settings.OTP_VALIDITY_MINUTES)
        return self.otp_created_at < expiry_limit

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.lower()
        if not self.username:
            self.username = self.email
        
        # Populate default timezone
        if not self.timezone_id:
            from onboard.models.timezone import get_default_timezone
            self.timezone_id = get_default_timezone()

        # Populate default language
        if not self.language_id:
            from onboard.models.language import get_default_language
            self.language_id = get_default_language()

        super().save(*args, **kwargs)

    def __str__(self):
        return self.email