"""
Keeping a host's availability profile aligned with their own timezone.

``CustomUser.timezone`` and ``AvailabilityProfile.timezone`` are separate fields
answering different questions — "where is this person" versus "what zone are these
working hours written in". They are usually the same, and when a host relocates
the second should follow the first: "I work 09:00–17:00" is meant to stay 9-to-5
where they now are, not shift by however many hours they moved.
"""

import logging

from django.db import transaction

logger = logging.getLogger(__name__)


def sync_profile_timezone(user, previous_timezone_id):
    """Move a host's availability profile onto their new timezone.

    Returns the updated profile, or ``None`` when nothing was changed.

    **Only when the two were already in step.** A profile whose timezone already
    differed from the user's was set that way on purpose — a host in Kolkata
    keeping New York hours for US customers is a real configuration, and silently
    re-anchoring it would move every slot they offer by nine and a half hours
    without anyone asking. The comparison against the *previous* user timezone is
    what tells those two cases apart; comparing against the new one cannot.

    A user who had no timezone at all is left alone for the same reason: there is
    no previous value to have been in step with, so there is no evidence the
    profile's zone was ever meant to track theirs.
    """
    from core.models.availability_profile import AvailabilityProfile

    if user.timezone_id is None or previous_timezone_id is None:
        return None
    if user.timezone_id == previous_timezone_id:
        return None

    profile = AvailabilityProfile.objects.filter(user=user).first()
    if profile is None:
        return None
    if profile.timezone_id != previous_timezone_id:
        logger.info(
            "availability profile %s keeps its own timezone; it already differed "
            "from the user's",
            profile.id,
        )
        return None

    profile.timezone_id = user.timezone_id
    profile.save(update_fields=["timezone", "updated_at"])

    # The wall-clock rules are unchanged, but they now resolve against a different
    # zone, so every materialized UTC window is wrong until this runs. Post-commit
    # because the task re-reads the profile on its own connection.
    from core.services import deferred
    from core.tasks import materialize_profile_task

    transaction.on_commit(
        deferred.safe(
            lambda: materialize_profile_task.delay(str(profile.id)),
            label=f"rematerialize profile {profile.id} after timezone change",
        )
    )

    logger.info(
        "availability profile %s moved to timezone %s following user %s",
        profile.id,
        user.timezone_id,
        user.id,
        extra={
            "profile_id": str(profile.id),
            "user_id": str(user.id),
            "timezone_id": str(user.timezone_id),
        },
    )
    return profile
