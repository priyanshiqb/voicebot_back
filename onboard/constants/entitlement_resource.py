from django.db import models


class EntitlementResource(models.TextChoices):
    """A countable resource a plan grants an allowance of.

    These are **gauges**, not credits: they are measured as current usage against a
    limit, so deleting a resource gives the allowance back. Nothing here ever touches
    the credit wallet — a wrong model here would be charging credits for an agent
    seat, and then owing a refund when the seat is deleted.

    Each one names a real, countable thing:

    ``AGENT``            ``core.Agent`` rows for the tenant
    ``USER``             ``tenant.TenantUserMapping`` rows
    ``CONTACT``          ``core.Contact`` rows
    ``KNOWLEDGE_BASE``   ``core.KnowledgeBase`` rows
    ``CRAWL_PAGE``       ``core.KBWebsite`` rows — one per crawled page
    ``CAMPAIGN_MONTHLY`` ``core.Campaign`` rows created in the current window
    ``STORAGE_GB``       summed file sizes, in GB

    ``CAMPAIGN_MONTHLY`` is the odd one: it is a *windowed* count rather than a
    standing total, so it is the one resource where "delete it and get the allowance
    back" does not hold. Three things about it are easy to get wrong:

    * **The window is configurable**, not monthly. The name is historical — the
      period comes from ``PlanEntitlement.reset_period`` and runs from daily to
      yearly. A plan can even leave it as a standing total, though that would be an
      odd way to sell campaigns.
    * **It is anchored to the tenant's subscription, not the calendar.** Anchoring to
      the 1st let a tenant whose cycle ran 5 Aug — 4 Sep collect two allowances inside
      one billing cycle.
    * **Counted from ``created_at``, forgiven on ``started_at``.** A campaign claims
      its slot when it is created, so the limit is felt before an audience is built;
      it gives the slot back only if it never ran — deleted, cancelled or failed to
      materialise. Once calls have gone out, nothing reclaims it.

    Still counted live from a date filter, so it needs no counter and no reset job.
    """

    AGENT = "agent", "Voicebot Agents"
    USER = "user", "User Accounts"
    CONTACT = "contact", "Contacts"
    KNOWLEDGE_BASE = "knowledge_base", "Knowledge Bases"
    CRAWL_PAGE = "crawl_page", "Crawled Website Pages"
    CAMPAIGN_MONTHLY = "campaign_monthly", "Campaigns Per Month"
    STORAGE_GB = "storage_gb", "Storage (GB)"
