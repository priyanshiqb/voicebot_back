from core.models.contact import Contact

from .base_resource import TenantModelResource

# Excel and LibreOffice treat a leading one of these as the start of a formula, so an
# exported contact named `=HYPERLINK(...)` executes when the file is opened. Contact
# names, companies, and notes are all user-supplied and round-trip through export.
CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_csv_value(value):
    """Neutralize spreadsheet formula injection without altering the visible text."""
    if isinstance(value, str) and value.startswith(CSV_INJECTION_PREFIXES):
        return f"'{value}"
    return value


class ContactResource(TenantModelResource):
    """
    Export-facing resource for contacts.

    Import does NOT go through here — contacts are imported via the staged
    ContactImportSession flow, which adds conflict review and phone normalization.
    """

    class Meta:
        model = Contact
        fields = (
            "id",
            "first_name",
            "last_name",
            "phone_number",
            "phone_e164",
            "email_address",
            "company",
            "tags",
            "notes",
            "source",
            "consent_status",
            "do_not_call",
            "is_active",
            "created_at",
        )
        export_order = fields

    def export_field(self, field, instance, **kwargs):
        return escape_csv_value(super().export_field(field, instance, **kwargs))
