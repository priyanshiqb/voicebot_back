from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MinLengthValidator
from django.db import models, transaction
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from tenant.common.models.tenant_base_model import TenantBaseModel

# Levels, not depth values: a 3-level tree stores `depth` 0, 1, 2.
#
# A module constant rather than a setting, deliberately. The bound is enforced by a
# CheckConstraint, and a constraint built from a runtime setting is baked into the
# migration at makemigrations time and then silently diverges the moment anyone edits
# the environment — the database keeps the old number while the code reports the new
# one. Same reasoning as MAX_HORIZON_DAYS on EventTemplate.
MAX_CATEGORY_DEPTH = 3
MAX_DEPTH_INDEX = MAX_CATEGORY_DEPTH - 1


class CatalogCategory(TenantBaseModel):
    """
    The tenant's own navigation tree over their catalog — "Menu → Starters",
    "Services → Diagnostics". Tier 2 (fully audited + soft delete).

    Self-nesting, capped at :data:`MAX_CATEGORY_DEPTH` levels. The cap is not
    squeamishness about recursion: an uncapped tree makes every "show me this
    section" query unbounded, and a category eleven levels down is a UI that cannot
    be rendered rather than a taxonomy anyone wanted.

    ``depth`` is **stored, not computed**. It makes the cap a column check instead of
    a walk to the root on every write, and it lets the whole tree be fetched and
    rendered in one query ordered by ``(depth, sort_order)``. The cost is that it has
    to be maintained when a category is re-parented — see :meth:`save`, which
    cascades to descendants.
    """

    name = models.CharField(
        max_length=150,
        validators=[MinLengthValidator(2), MaxLengthValidator(150)],
        help_text=_("Section name, e.g. 'Starters' or 'Diagnostics'."),
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        help_text=_(
            "Parent section. Null makes this a root category. PROTECT guards the "
            "hard-delete path; the soft-delete path is guarded in `soft_delete`."
        ),
    )
    depth = models.PositiveSmallIntegerField(
        default=0,
        editable=False,
        help_text=_(
            "0 for a root category, 1 for its children, and so on. Derived on save "
            "from `parent` — never set this directly."
        ),
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional blurb shown with the section."),
    )
    sort_order = models.PositiveIntegerField(
        default=0,
        help_text=_(
            "Manual ordering among siblings. Restaurants care intensely about the "
            "order menu sections appear in, and alphabetical is never it."
        ),
    )

    class Meta:
        db_table = "core_catalog_categories"
        verbose_name = _("Catalog Category")
        verbose_name_plural = _("Catalog Categories")
        ordering = ["depth", "sort_order", "name"]
        constraints = [
            # Two constraints rather than one, because Postgres treats NULLs as
            # distinct inside a unique index: a single constraint over
            # (tenant, lower(name), parent) would happily allow two root categories
            # both called "Services", which is the exact duplicate a user would
            # report as a bug.
            #
            # UniqueConstraint(nulls_distinct=False) would collapse these into one on
            # PG 15+, but the deployment tracks `postgres:latest` — an unpinned image
            # is not something to hang a correctness guarantee on.
            models.UniqueConstraint(
                Lower("name"),
                "tenant",
                condition=models.Q(is_deleted=False, parent__isnull=True),
                name="unique_active_root_catalog_category_tenant",
            ),
            models.UniqueConstraint(
                Lower("name"),
                "tenant",
                "parent",
                condition=models.Q(is_deleted=False, parent__isnull=False),
                name="unique_active_child_catalog_category_parent",
            ),
            models.CheckConstraint(
                condition=models.Q(depth__gte=0) & models.Q(depth__lte=MAX_DEPTH_INDEX),
                name="catalog_category_depth_bounded",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "parent", "sort_order"]),
        ]

    def __str__(self):
        return self.name

    # --- tree helpers ---------------------------------------------------------

    def resolved_depth(self):
        """What ``depth`` should be, given the current ``parent``."""
        if self.parent_id is None:
            return 0
        return self.parent.depth + 1

    def subtree_height(self):
        """How many levels hang below this node. 0 when it has no children.

        Needed by :meth:`clean`: moving a category with grandchildren under a
        depth-1 parent would push those grandchildren to depth 3. Validating only
        this node's own depth would let that through, and the descendants would then
        fail the check constraint during the cascade — a write that half-succeeds.
        """
        children = list(CatalogCategory.objects.filter(parent_id=self.pk))
        if not children:
            return 0
        return 1 + max(child.subtree_height() for child in children)

    def _cascade_depth_to_descendants(self):
        """Re-stamp ``depth`` on everything below this node after a re-parent.

        Uses ``all_objects`` and a queryset ``update``: soft-deleted descendants must
        stay consistent too (they can be restored), and ``depth`` is derived, so
        running each child through ``save()`` would buy nothing but round trips.
        """
        children = list(CatalogCategory.all_objects.filter(parent_id=self.pk))
        if not children:
            return

        expected = self.depth + 1
        CatalogCategory.all_objects.filter(parent_id=self.pk).update(depth=expected)
        for child in children:
            child.depth = expected
            child._cascade_depth_to_descendants()

    # --- validation -----------------------------------------------------------

    def clean(self):
        super().clean()

        parent = self.parent
        if parent is None:
            depth = 0
        else:
            # A tenant must not be able to graft its category under a stranger's tree
            # by naming an id. Same class of hole as TenantSettings.clean().
            if parent.tenant_id != self.tenant_id:
                raise ValidationError(
                    {"parent": _("That category belongs to a different tenant.")}
                )
            if self.pk is not None and parent.pk == self.pk:
                raise ValidationError(
                    {"parent": _("A category cannot be its own parent.")}
                )

            # Walk to the root looking for self. `seen` guards against spinning
            # forever on a cycle that already exists in the data.
            seen = set()
            node = parent
            while node is not None:
                if self.pk is not None and node.pk == self.pk:
                    raise ValidationError(
                        {
                            "parent": _(
                                "That category is below this one — moving it there "
                                "would create a loop."
                            )
                        }
                    )
                if node.pk in seen:
                    break
                seen.add(node.pk)
                node = node.parent

            depth = parent.depth + 1

        if depth > MAX_DEPTH_INDEX:
            raise ValidationError(
                {
                    "parent": _(
                        "Categories can be nested %(levels)d levels deep at most."
                    )
                    % {"levels": MAX_CATEGORY_DEPTH}
                }
            )

        if self.pk is not None and depth + self.subtree_height() > MAX_DEPTH_INDEX:
            raise ValidationError(
                {
                    "parent": _(
                        "This category has sub-categories of its own; moving it there "
                        "would nest them deeper than %(levels)d levels."
                    )
                    % {"levels": MAX_CATEGORY_DEPTH}
                }
            )

    # --- persistence ----------------------------------------------------------

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")

        # Derive whenever `parent` is in play. Targeted housekeeping saves
        # (soft_delete, activate, restore) name their fields and skip this, so a row
        # can always be removed or deactivated without touching the tree.
        depth_changed = False
        if update_fields is None or "parent" in update_fields:
            previous_depth = self.depth
            self.depth = self.resolved_depth()
            depth_changed = self.depth != previous_depth
            if update_fields is not None and depth_changed:
                kwargs["update_fields"] = list(
                    dict.fromkeys([*update_fields, "depth"])
                )

        super().save(*args, **kwargs)

        if depth_changed:
            self._cascade_depth_to_descendants()

    def soft_delete(self, user=None):
        """Remove the section, refusing while anything still lives in it.

        ``PROTECT`` on the FK cannot cover this. The row is soft-deleted, so the
        database constraint never fires, and the children and items would be left
        pointing at a section the UI no longer lists — items silently vanishing from
        the catalog while still being sellable.

        The check lives here rather than in the viewset so every path gets it: the
        REST delete, a `bulk_delete` that calls `soft_delete` per row, the admin, and
        a shell session alike. It runs under the row lock so a category created
        concurrently underneath cannot slip past it.
        """
        with transaction.atomic():
            CatalogCategory.objects.select_for_update().get(pk=self.pk)

            blocking_children = self.children.count()
            blocking_items = self.items.count()
            if blocking_children or blocking_items:
                parts = []
                if blocking_children:
                    parts.append(f"{blocking_children} sub-categor(y/ies)")
                if blocking_items:
                    parts.append(f"{blocking_items} item(s)")
                raise ValidationError(
                    f"This category still contains {' and '.join(parts)}. Move or "
                    "delete them first, or set is_active=false to hide the section "
                    "while keeping its contents."
                )

            super().soft_delete(user=user)

    def restore(self):
        """Un-delete, refusing to create a duplicate.

        The unique constraints only cover live rows, so the name freed by deleting
        this category may have been taken since. Without this the restore raises a
        raw IntegrityError out of the admin. Mirrors ``Contact.restore()``.
        """
        clash = (
            CatalogCategory.objects.filter(
                tenant_id=self.tenant_id,
                parent_id=self.parent_id,
                name__iexact=self.name,
            )
            .exclude(pk=self.pk)
            .exists()
        )
        if clash:
            raise ValidationError(
                {
                    "name": _(
                        "Another active category at this level already uses this "
                        "name. Rename or remove it before restoring this one."
                    )
                }
            )
        super().restore()
