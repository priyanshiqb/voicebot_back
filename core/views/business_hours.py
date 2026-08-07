import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status

from core.models.business_hours import BusinessHours
from core.serializers.business_hours import BusinessHoursSerializer
from onboard.models.timezone import Timezone
from tenant.common.views.tenant_base_api_view import TenantBaseAPIView

logger = logging.getLogger(__name__)


@extend_schema(tags=["Business Hours"])
class BusinessHoursView(TenantBaseAPIView):
    """
    When the business is open, read and written as a **singleton** — one row per
    tenant, so no id in the path and no list.

    The row is created on first read, so a fresh tenant gets a renderable seven-day
    grid rather than a 404 the client has to special-case. It starts open, with no
    intervals — which reads as "closed every day" until hours are set, the honest
    answer for a business that has not told us anything.

    Not to be confused with `/core/availability-profiles/`, which is when an
    individual **person** is free. A bot answering out of hours and a host on leave
    are different facts about different subjects.
    """

    def _hours(self):
        tenant = self.get_tenant()
        if tenant is None:
            # A superuser with no tenant mapping. There is no "all tenants' hours"
            # answer to give, so say so rather than inventing a row.
            return None

        hours = BusinessHours.objects.filter(tenant=tenant).first()
        if hours is not None:
            return hours

        # UTC as the seed, never a guess at the tenant's real zone: a schedule
        # silently anchored to the wrong timezone is worse than an obviously
        # unset one, because every time in it is quietly wrong by hours.
        utc, _ = Timezone.objects.get_or_create(
            timezone="UTC", defaults={"offset": "UTC+00:00", "region": "UTC"}
        )
        return BusinessHours.objects.create(tenant=tenant, timezone=utc)

    def _no_tenant(self):
        return self.error(
            message="No tenant context. Superusers must act within a tenant.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @extend_schema(responses={200: BusinessHoursSerializer})
    def get(self, request):
        hours = self._hours()
        if hours is None:
            return self._no_tenant()

        serializer = BusinessHoursSerializer(hours, context={"request": request})
        return self.success(data=serializer.data, message="Retrieved successfully")

    @extend_schema(
        request=BusinessHoursSerializer, responses={200: BusinessHoursSerializer}
    )
    def patch(self, request):
        hours = self._hours()
        if hours is None:
            return self._no_tenant()

        serializer = BusinessHoursSerializer(
            hours,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)

        logger.info(
            "business hours updated",
            extra={"tenant_id": str(hours.tenant_id), "user_id": str(request.user.id)},
        )
        return self.success(data=serializer.data, message="Updated successfully")
