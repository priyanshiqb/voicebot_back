"""
The documented shape of an upload ticket.

Every ``request-upload-url`` endpoint returns the same envelope from
``core.services.uploads.build_upload_ticket``. Each one used to declare its own
``inline_serializer`` listing ``upload_url`` and ``object_key`` and nothing else, which
was accurate right up until the ticket grew the fields a client has to branch on — and
then four separate places had to be remembered.

Declaring the fields once means the generated schema cannot drift from what the views
actually return, and a client generating an SDK from it sees ``upload_method`` rather
than discovering at runtime that a raw PUT no longer works.
"""

from rest_framework import serializers

from core.services.uploads import UPLOAD_METHODS


def upload_ticket_fields():
    """Field definitions shared by every ``request-upload-url`` response.

    Returned as a dict rather than a serializer class so each flow can splice its own
    handle (``file_id``, ``media_id``) alongside them while keeping a distinct component
    name in the schema — drf-spectacular rejects two components with the same name.
    """
    return {
        "upload_method": serializers.ChoiceField(
            choices=UPLOAD_METHODS,
            help_text=(
                "How to send the file. POST: multipart form with `upload_fields`, file "
                "part last, success is 204. PUT: raw body with the declared "
                "Content-Type, success is 200. Deployment configuration — branch on it "
                "rather than hardcoding."
            ),
        ),
        "upload_url": serializers.CharField(
            help_text="Where to send the file. Goes to the object store, not this API."
        ),
        "upload_fields": serializers.DictField(
            child=serializers.CharField(),
            help_text=(
                "POST only: signed policy fields to send as form fields before the file "
                "part. Empty under PUT."
            ),
        ),
        "expires_in_minutes": serializers.IntegerField(
            help_text="Ticket lifetime. Request a new one rather than caching it."
        ),
        "max_bytes": serializers.IntegerField(
            help_text=(
                "Server-side ceiling for this flow. Signed into the policy and enforced "
                "during the upload under POST; checked at confirm-upload either way."
            )
        ),
    }
