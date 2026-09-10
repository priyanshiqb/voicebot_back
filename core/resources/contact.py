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

    # The exported columns, in order. `id` and `phone_e164` are deliberately absent:
    # both are internal. `id` is a UUID nobody re-keys by hand, and `phone_e164` is the
    # normalized twin of `phone_number` — two columns of the same number in every file.
    #
    # Removing them from Meta.fields alone would NOT drop them. `id` is declared on
    # TenantModelResource (import_id_fields resolves through self.fields, so it must
    # be), and _get_ordered_field_names appends every declared field that Meta.fields
    # omitted — relocating the column to the end rather than removing it. EXPORT_FIELDS
    # is the switch that actually decides. Import is unaffected: it reads whatever
    # columns the uploaded file has, and `id` still resolves an existing row.
    EXPORT_FIELDS = (
        "first_name",
        "last_name",
        "phone_number",
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
