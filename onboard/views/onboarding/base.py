from rest_framework.permissions import BasePermission
from django.conf import settings
import razorpay
from onboard.models import TempUser

# Initialize Razorpay Client safely
RAZORPAY_KEY_ID = getattr(settings, "RAZORPAY_KEY_ID", "rzp_test_dummykey")
RAZORPAY_KEY_SECRET = getattr(settings, "RAZORPAY_KEY_SECRET", "dummysecret")

try:
    razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
except Exception:
    razorpay_client = None


class IsTempUserAuthenticated(BasePermission):
    """
    Permission check to verify if the request was authenticated with a TempUser.
    """
    def has_permission(self, request, view):
        return isinstance(request.user, TempUser)


def create_tenant_and_mapping(temp_user, user):
    """
    Creates a new Tenant organization for the onboarded registrant,
    populates referral code tracking, and maps the user to the tenant.
    """
    import re
    from tenant.models import Tenant, TenantUserMapping

    # 1. Generate a clean and unique subdomain from business_name
    business_name = temp_user.business_name or "business"
    subdomain = re.sub(r'[^a-zA-Z0-9-]', '', business_name.lower().replace(" ", "-"))
    if not subdomain:
        subdomain = "tenant"
        
    base_subdomain = subdomain
    counter = 1
    while Tenant.objects.filter(subdomain=subdomain).exists():
        subdomain = f"{base_subdomain}-{counter}"
        counter += 1

    # 2. Create the Tenant with referred_by_code
    tenant = Tenant.objects.create(
        name=temp_user.business_name,
        subdomain=subdomain,
        referred_by_code=temp_user.referral_code,
        timezone=temp_user.timezone
    )

    # 3. Establish 1:1 mapping between User and Tenant
    TenantUserMapping.objects.create(
        user=user,
        tenant=tenant
    )
    return tenant
