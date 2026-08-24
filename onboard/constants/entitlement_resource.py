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
    ``CAMPAIGN_MONTHLY`` ``core.Campaign`` rows started this calendar month
    ``STORAGE_GB``       summed file sizes, in GB

    ``CAMPAIGN_MONTHLY`` is the odd one: it is a *windowed* count that resets each
    month rather than a standing total. Still counted live from a date filter, so it
    needs no counter and no reset job — but it is the one resource where "delete it
    and get the allowance back" does not hold.
    """

    AGENT = "agent", "Voicebot Agents"
    USER = "user", "User Accounts"
    CONTACT = "contact", "Contacts"
    KNOWLEDGE_BASE = "knowledge_base", "Knowledge Bases"
    CRAWL_PAGE = "crawl_page", "Crawled Website Pages"
    CAMPAIGN_MONTHLY = "campaign_monthly", "Campaigns Per Month"
    STORAGE_GB = "storage_gb", "Storage (GB)"
