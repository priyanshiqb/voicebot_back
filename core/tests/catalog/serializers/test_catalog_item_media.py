"""
Media serializers, including the two-step upload contract.

The security-relevant assertions are in `UploadRequestTests`. The upload flow is
stateful specifically so a client never names an object key — the stateless
alternative is what let one tenant confirm *and delete* another tenant's Knowledge
Base files. Two tests pin the parts of that contract a refactor could quietly undo:

* a cross-tenant `item` is refused, and the message says **"No such item"** rather
  than "that belongs to someone else" — a 403-shaped message confirms the row exists;
* `upload_status` is not writable, because a client able to flip it to CONFIRMED could
  publish a key with nothing behind it.
"""

from core.models.catalog_item_media import CatalogItemMedia
from core.serializers.catalog_item_media import (
    ALLOWED_EXTENSIONS,
    CatalogItemMediaListSerializer,
    CatalogItemMediaRefSerializer,
    CatalogItemMediaSerializer,
    CatalogMediaUploadConfirmSerializer,
    CatalogMediaUploadRequestSerializer,
    presigned_media_url,
)
from core.tests.catalog.serializers.base import CatalogSerializerTestCase

MediaType = CatalogItemMedia.MediaType
UploadStatus = CatalogItemMedia.UploadStatus


class MediaSerializerTestCase(CatalogSerializerTestCase):
    def setUp(self):
        super().setUp()
        self.item = self.make_item("Nike Air Max")

    def make_media(self, **overrides):
        fields = {
            "tenant": self.tenant,
            "item": self.item,
            "media_type": MediaType.IMAGE,
            "original_name": "photo.jpg",
            "extension": ".jpg",
            "file_size_in_bytes": 1024,
            "upload_status": UploadStatus.CONFIRMED,
        }
        fields.update(overrides)
        return CatalogItemMedia.objects.create(**fields)

    def upload_payload(self, **overrides):
        data = {
            "item": str(self.item.pk),
            "media_type": MediaType.IMAGE,
            "filename": "photo.jpg",
            "content_type": "image/jpeg",
            "file_size_in_bytes": 1024,
        }
        data.update(overrides)
        return data

    def build_request(self, **overrides):
        return self.build(
            CatalogMediaUploadRequestSerializer, self.upload_payload(**overrides)
        )


class UploadRequestTests(MediaSerializerTestCase):
    def test_a_valid_request_passes_and_derives_the_extension(self):
        serializer = self.build_request()
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))
        self.assertEqual(serializer.validated_data["extension"], ".jpg")

    def test_the_extension_is_derived_case_insensitively(self):
        serializer = self.build_request(filename="PHOTO.JPG")
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))
        self.assertEqual(serializer.validated_data["extension"], ".jpg")

    def test_a_cross_tenant_item_reads_as_not_found(self):
        """404-shaped, not 403-shaped. Confirming that another tenant's item exists is
        itself a leak, so the message must not distinguish "yours but wrong" from
        "not yours"."""
        foreign_item = self.make_item("Theirs", tenant=self.other_tenant)
        serializer = self.build(
            CatalogMediaUploadRequestSerializer,
            self.upload_payload(item=str(foreign_item.pk)),
        )
        self.assert_field_error(serializer, "item", "No such item")

    def test_an_extension_outside_the_allowlist_is_rejected(self):
        """Checked before a presigned URL is issued, so an unwanted type never reaches
        the bucket at all."""
        serializer = self.build_request(filename="malware.exe")
        self.assert_field_error(serializer, "filename", "not a supported")

    def test_a_filename_with_no_extension_is_rejected(self):
        serializer = self.build_request(filename="photo")
        self.assert_field_error(serializer, "filename")

    def test_the_allowlist_is_per_media_type(self):
        """A .pdf is a fine document and not a fine image."""
        serializer = self.build_request(
            media_type=MediaType.IMAGE, filename="spec.pdf"
        )
        self.assert_field_error(serializer, "filename")

        serializer = self.build_request(
            media_type=MediaType.DOCUMENT, filename="spec.pdf"
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_every_media_type_has_an_allowlist(self):
        """A type with no entry would raise KeyError inside `validate()` — a 500 on a
        perfectly ordinary request."""
        for media_type in MediaType.values:
            with self.subTest(media_type=media_type):
                self.assertIn(media_type, ALLOWED_EXTENSIONS)
                self.assertTrue(ALLOWED_EXTENSIONS[media_type])

    def test_a_representative_extension_is_accepted_for_each_type(self):
        for media_type, filename in (
            (MediaType.IMAGE, "photo.webp"),
            (MediaType.VIDEO, "clip.mp4"),
            (MediaType.DOCUMENT, "sheet.xlsx"),
        ):
            with self.subTest(media_type=media_type):
                serializer = self.build_request(
                    media_type=media_type, filename=filename
                )
                self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_an_oversize_file_is_rejected(self):
        with self.settings(CATALOG_MAX_MEDIA_BYTES=1024):
            serializer = self.build_request(file_size_in_bytes=2048)
            self.assert_field_error(serializer, "file_size_in_bytes", "MB per file")

    def test_a_zero_byte_file_is_rejected(self):
        serializer = self.build_request(file_size_in_bytes=0)
        self.assert_field_error(serializer, "file_size_in_bytes")

    def test_a_blank_filename_is_rejected(self):
        serializer = self.build_request(filename="   ")
        self.assert_field_error(serializer, "filename")

    def test_the_per_item_cap_is_enforced(self):
        with self.settings(CATALOG_MAX_MEDIA_PER_ITEM=1):
            self.make_media()
            serializer = self.build_request()
            self.assert_field_error(serializer, "item", "maximum")

    def test_unconfirmed_uploads_count_toward_the_cap(self):
        """Otherwise requesting a hundred upload URLs and never confirming them is an
        unmetered way past it."""
        with self.settings(CATALOG_MAX_MEDIA_PER_ITEM=1):
            self.make_media(upload_status=UploadStatus.PENDING)
            serializer = self.build_request()
            self.assert_field_error(serializer, "item", "unconfirmed uploads count")

    def test_a_soft_deleted_media_row_does_not_count(self):
        with self.settings(CATALOG_MAX_MEDIA_PER_ITEM=1):
            media = self.make_media()
            media.soft_delete()
            serializer = self.build_request()
            self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_optional_fields_default(self):
        serializer = self.build_request()
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))
        self.assertEqual(serializer.validated_data["alt_text"], "")
        self.assertFalse(serializer.validated_data["is_primary"])

    def test_media_type_defaults_to_image(self):
        payload = self.upload_payload()
        payload.pop("media_type")
        serializer = self.build(CatalogMediaUploadRequestSerializer, payload)
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))
        self.assertEqual(serializer.validated_data["media_type"], MediaType.IMAGE)

    def test_the_response_contract_contains_no_object_key(self):
        """The whole point of the stateful flow. Asserted on the serializer's own
        fields so that adding an `object_key` output here fails loudly."""
        serializer = self.build_request()
        self.assertNotIn("object_key", serializer.fields)
        self.assertNotIn("file", serializer.fields)


class UploadConfirmTests(MediaSerializerTestCase):
    def test_a_media_id_is_required(self):
        serializer = CatalogMediaUploadConfirmSerializer(data={})
        self.assertFalse(serializer.is_valid())
        self.assertIn("media_id", serializer.errors)

    def test_a_malformed_media_id_is_rejected(self):
        serializer = CatalogMediaUploadConfirmSerializer(data={"media_id": "not-a-uuid"})
        self.assertFalse(serializer.is_valid())
        self.assertIn("media_id", serializer.errors)

    def test_a_valid_media_id_passes(self):
        media = self.make_media()
        serializer = CatalogMediaUploadConfirmSerializer(
            data={"media_id": str(media.pk)}
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_the_body_accepts_nothing_else(self):
        """`media_id` and nothing else, by design — no key may cross the wire."""
        self.assertEqual(
            set(CatalogMediaUploadConfirmSerializer().fields), {"media_id"}
        )


class MediaSerializerFieldTests(MediaSerializerTestCase):
    def test_the_upload_managed_fields_are_read_only(self):
        media = self.make_media(upload_status=UploadStatus.PENDING)
        serializer = self.build(
            CatalogItemMediaSerializer,
            {
                "upload_status": UploadStatus.CONFIRMED,
                "file_size_in_bytes": 999,
                "extension": ".png",
                "original_name": "hacked.png",
                "alt_text": "A shoe",
            },
            instance=media,
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))
        updated = serializer.save()

        self.assertEqual(updated.upload_status, UploadStatus.PENDING)
        self.assertEqual(updated.file_size_in_bytes, 1024)
        self.assertEqual(updated.extension, ".jpg")
        self.assertEqual(updated.original_name, "photo.jpg")
        self.assertEqual(updated.alt_text, "A shoe")

    def test_the_item_is_not_reassignable(self):
        """Moving a photo between items would leave its object key under the old
        item's prefix, so the key and the row would disagree about where it lives."""
        media = self.make_media()
        other_item = self.make_item("Something Else")
        serializer = self.build(
            CatalogItemMediaSerializer,
            {"item": str(other_item.pk)},
            instance=media,
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))
        self.assertEqual(serializer.save().item_id, self.item.pk)

    def test_alt_text_is_stripped(self):
        media = self.make_media()
        serializer = self.build(
            CatalogItemMediaSerializer,
            {"alt_text": "  A shoe  "},
            instance=media,
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))
        self.assertEqual(serializer.save().alt_text, "A shoe")

    def test_the_writable_fields_are_only_the_presentation_ones(self):
        writable = {
            name
            for name, field in CatalogItemMediaSerializer().fields.items()
            if not field.read_only
        }
        self.assertEqual(writable, {"alt_text", "is_primary", "sort_order", "is_active"})


class RepresentationTests(MediaSerializerTestCase):
    def test_a_row_with_no_file_has_a_null_url(self):
        media = self.make_media()
        self.assertIsNone(presigned_media_url(media))
        self.assertIsNone(CatalogItemMediaRefSerializer(media).data["file_url"])

    def test_the_stored_name_is_returned_when_s3_is_off(self):
        media = self.make_media()
        media.file.name = "t-x/catalog/y/photo.jpg"
        media.save(update_fields=["file"])

        with self.settings(USE_S3_STORAGE=False):
            self.assertEqual(presigned_media_url(media), "t-x/catalog/y/photo.jpg")

    def test_the_ref_serializer_is_minimal(self):
        data = CatalogItemMediaRefSerializer(self.make_media()).data
        self.assertEqual(
            set(data),
            {"id", "media_type", "file_url", "alt_text", "is_primary", "sort_order"},
        )

    def test_the_list_serializer_drops_the_tenant_block(self):
        data = CatalogItemMediaListSerializer(self.make_media()).data
        self.assertEqual(data["original_name"], "photo.jpg")
        self.assertEqual(data["upload_status"], UploadStatus.CONFIRMED)
        self.assertNotIn("tenant", data)

    def test_the_main_serializer_labels_the_media_type(self):
        data = CatalogItemMediaSerializer(
            self.make_media(media_type=MediaType.DOCUMENT), context=self.context()
        ).data
        self.assertEqual(data["media_type_label"], "Document")
