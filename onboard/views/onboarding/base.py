from rest_framework.permissions import BasePermission
from django.conf import settings
import razorpay
from onboard.models import TempUser

# Settings raise at import if these are unset, so there is nothing to fall back to —
# and a getattr default here would reintroduce exactly the hole the settings removed.
RAZORPAY_KEY_ID = settings.RAZORPAY_KEY_ID
RAZORPAY_KEY_SECRET = settings.RAZORPAY_KEY_SECRET

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


def billing_breakdown(plan, minutes=None):
    """What the registrant will be charged, and what they get for it.

    **One implementation, four callers** — verify-otp, update-plan and both
    resume-session views all showed the customer a price, and all four built the same
    dict by hand off the plan's catalogue price. That was survivable while a plan had one
    price; with the price depending on a quantity it would be four chances to show a
    figure that does not match the order.

    Falls back to the plan's catalogue price for a legacy plan, which is exactly what
    those four hand-rolled dicts did.
    """
    price = plan.price_components(minutes)
    return {
        "base_price": str(price.gross),
        "discount_amount": str(price.discount),
        "net_base_price": str(price.net),
        "tax_percentage": str(plan.tax_percentage),
        "tax_amount": str(price.tax),
        "total_price": str(price.total),
        "currency": plan.currency,
        # Null on a legacy plan, so a client can tell the two apart without
        # inspecting the plan.
        "selected_minutes": minutes,
        "price_per_minute_inr": (
            str(plan.basis_rate().price_per_minute_inr)
            if plan.is_minutes_based
            else None
        ),
        "allocated_credits": plan.credits_for(minutes),
    }


def empty_billing_breakdown():
    """The shape returned when a registrant has no plan selected yet."""
    return {
        "base_price": "0.00",
        "discount_amount": "0.00",
        "net_base_price": "0.00",
        "tax_percentage": "0.00",
        "tax_amount": "0.00",
        "total_price": "0.00",
        "currency": "INR",
        "selected_minutes": None,
        "price_per_minute_inr": None,
        "allocated_credits": 0,
    }


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
        phone_number=temp_user.phone_number,
        referred_by_code=temp_user.referral_code,
        timezone=temp_user.timezone,
        language=temp_user.language
    )

    # 3. Establish 1:1 mapping between User and Tenant
    # OWNER, explicitly. The registrant is the first and only person at this tenant,
    # and the role default is MEMBER — which would leave a brand-new business unable
    # to invite anyone, and the tenant with no owner to appoint one.
    TenantUserMapping.objects.create(
        user=user,
        tenant=tenant,
        role=TenantUserMapping.Role.OWNER,
    )
    return tenant
