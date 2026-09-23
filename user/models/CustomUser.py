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

    class PlatformRole(models.TextChoices):
        """Who runs the admin console.

        Two roles, because two is what the work currently divides into and a
        permission matrix nobody can hold in their head is one that gets bypassed
        with a superuser account.

        ``MANAGER`` runs the whole panel. No per-action grants are consulted, and
        none are needed -- a manager can do everything the console offers.

        ``SUB_MANAGER`` starts able to *read* and nothing else. Each further action
        is granted individually on ``console_permissions``. Starting closed rather
        than open means a new capability is unreachable until somebody decides who
        should have it, instead of silently appearing for everyone.
        """

        MANAGER = "MANAGER", "Manager"
        SUB_MANAGER = "SUB_MANAGER", "Sub-manager"

    user_type = models.CharField(
        max_length=10,
        choices=UserType.choices,
        default=UserType.USER,
        help_text="Role/Type classification of the user account."
    )

    platform_role = models.CharField(
        max_length=16,
        choices=PlatformRole.choices,
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "What this operator may do in the admin console. **Null means not "
            "platform staff at all**, which is the state of every tenant user and "
            "the check the /ops/ namespace gates on. Setting it requires "
            "user_type=ADMIN, enforced by a database constraint so the two markers "
            "can never disagree."
        ),
    )

    console_permissions = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Actions a sub-manager has been granted, as a list of codes from "
            "OperatorAuditLog.Action \u2014 'credits.grant', 'payment.replay' and so "
            "on. **Ignored for a manager**, who may do everything. Keyed on the same "
            "vocabulary the audit log uses, so what somebody may do and what gets "
            "recorded when they do it can never drift apart."
        ),
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

    class Meta(AbstractUser.Meta):
        # Held by a person, not implied by a role: reading the API schema is its own
        # decision, separate from Django admin (`is_staff`) and from the operator
        # console (`platform_role`). See user.permissions.CanViewAPIDocs.
        permissions = [("view_api_docs", "Can view the API documentation")]
        # Inherited, not replaced. A bare `class Meta` here silently drops
        # AbstractUser's verbose_name and verbose_name_plural, which shows up as
        # "Custom users" in the admin and a spurious AlterModelOptions in the
        # migration -- a small thing that looks like somebody meant it.
        constraints = [
            # Two markers for one fact is how they drift. `platform_role` is the
            # gate the /ops/ namespace reads; `user_type` is what the rest of the
            # codebase already switches on. Tying them together in the database
            # means a half-configured operator cannot exist -- rather than existing
            # and being denied access for a reason nobody can find.
            models.CheckConstraint(
                condition=models.Q(platform_role__isnull=True)
                | models.Q(user_type="ADMIN"),
                name="ck_platform_role_requires_admin_user_type",
            ),
        ]

    @property
    def is_platform_staff(self):
        """Whether this account may reach the admin console at all.

        Deliberately not ``is_staff`` or ``is_superuser``: those grant Django admin,
        which is engineering's escape hatch and a different thing entirely. A support
        agent should have this and neither of those.
        """
        return self.platform_role is not None

    @property
    def is_console_manager(self):
        """Whether this operator runs the panel, rather than holding grants on it."""
        return self.platform_role == self.PlatformRole.MANAGER

    def can(self, action):
        """Whether this operator may perform ``action``.

        ``action`` is a code from ``OperatorAuditLog.Action``. A manager may do
        anything; a sub-manager may do what has been granted to them and nothing
        else. Reading is not gated here -- the namespace permission already
        establishes that, and a console where support cannot look at a tenant is not
        a console.
        """
        if self.platform_role is None:
            return False
        if self.is_console_manager:
            return True
        return str(action) in (self.console_permissions or [])

    def grant_console(self, *actions):
        """Add permissions to a sub-manager. Returns the resulting list.

        Does not save -- the caller decides when, and an audited action should be
        writing its own log row in the same transaction.
        """
        held = list(self.console_permissions or [])
        for action in actions:
            if str(action) not in held:
                held.append(str(action))
        self.console_permissions = held
        return held

    def revoke_console(self, *actions):
        """Remove permissions from a sub-manager. Returns the resulting list."""
        drop = {str(a) for a in actions}
        self.console_permissions = [
            a for a in (self.console_permissions or []) if a not in drop
        ]
        return self.console_permissions

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