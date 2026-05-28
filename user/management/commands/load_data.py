import json
import logging
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management import BaseCommand
from django.db import transaction
from user.models import CustomUser

logger = logging.getLogger("django")


def remove_duplicates(data_list):
    seen = []
    unique = []
    for item in data_list:
        if item not in seen:
            seen.append(item)
            unique.append(item)
    return unique


class Command(BaseCommand):
    """
    WARNING: Running this command is fully idempotent and safe to execute multiple times.
    It will locate existing records matching the 'unique_fields' constraints, perform updates
    on modified values, and create new records for non-existent indexes.

    Usage:
        python manage.py load_data all
        python manage.py load_data plan
    """

    help = "Load data from JSON files into the current database idempotently"
    command_name = "python manage.py load_data {file_name}"

    def add_arguments(self, parser):
        parser.add_argument("name", type=str, help="Name of the file for loading data")

    def handle(self, *args, **kwargs):
        try:
            self.is_dump_data = {}
            file_name = kwargs.get("name")

            with transaction.atomic():
                if file_name == "all":
                    with open(
                        f"{settings.BASE_DIR}/json_files/{file_name}.json", "r", encoding="utf-8"
                    ) as file:
                        all_file_name = json.load(file)["all_file_name"]
                else:
                    all_file_name = [file_name]

                for name in all_file_name:
                    self.is_dump_data[name] = False
                    self.insert_data(name)

                    if name == "group":
                        admin_group = Group.objects.filter(name="Admin").first()
                        if admin_group:
                            all_permissions = Permission.objects.all()
                            admin_group.permissions.set(all_permissions)
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"SUCCESS: Assigned {all_permissions.count()} permissions to Admin group"
                                )
                            )
        except Exception as e:
            logger.exception(str(e))
            self.stdout.write(self.style.ERROR(f"ERROR: {str(e)}"))

    def insert_data(self, name):
        try:
            try:
                with open(f"{settings.BASE_DIR}/json_files/{name}.json", "r", encoding="utf-8") as file:
                    file_data = json.load(file)
            except Exception as e:
                logger.exception(str(e))
                self.stdout.write(self.style.ERROR(f"ERROR: {str(e)}"))
                return

            with transaction.atomic():
                content_type = ContentType.objects.get(
                    app_label=file_data["app_label"], model=file_data["model"]
                )
                insert_model = content_type.model_class()
                unique_fields = file_data.get("unique_fields")
                all_fk_model = file_data.get("fk", [])

                # Get model field info to identify ManyToMany fields
                model_fields = insert_model._meta.get_fields()
                m2m_fields = {f.name: f for f in model_fields if f.many_to_many}

                # Enhanced FK model dictionary to support multiple lookup fields
                fk_model_dict = {}
                for fk_model_info in all_fk_model:
                    fk_content_type = ContentType.objects.get(
                        app_label=fk_model_info["app_label"],
                        model=fk_model_info["model"],
                    )
                    fk_model_dict[fk_model_info["field"]] = {
                        "model": fk_content_type.model_class(),
                        "lookup_fields": fk_model_info.get("lookup_fields", [fk_model_info.get("fk_field", "id")]),
                        # Backward compatibility: support old "fk_field" format
                        "fk_field": fk_model_info.get("fk_field")
                    }

                file_data["data"] = remove_duplicates(file_data["data"])

                # Create objects with their ManyToMany relationships
                self.create_objects_with_m2m(
                    insert_model,
                    file_data["data"],
                    unique_fields,
                    fk_model_dict,
                    m2m_fields,
                    name,
                    file_data["app_label"],
                    file_data["model"],
                )

        except Exception as e:
            logger.exception(str(e))
            self.stdout.write(self.style.ERROR(f"ERROR: {str(e)}"))

    def resolve_foreign_key_id(self, model_info, fk_data):
        """
        Resolve foreign key ID based on multiple lookup fields
        """
        try:
            model = model_info["model"]
            lookup_fields = model_info["lookup_fields"]

            # Handle backward compatibility with single fk_field
            if model_info.get("fk_field") and not isinstance(fk_data, dict):
                filter_kwargs = {model_info["fk_field"]: fk_data}
            else:
                # Build filter kwargs from lookup fields and fk_data
                filter_kwargs = {}
                for field in lookup_fields:
                    if field in fk_data:
                        filter_kwargs[field] = fk_data[field]
                    else:
                        self.stdout.write(
                            self.style.WARNING(
                                f"WARNING: Lookup field '{field}' not found in FK data: {fk_data}"
                            )
                        )
                        return None

            if not filter_kwargs:
                self.stdout.write(
                    self.style.WARNING(
                        f"WARNING: No valid lookup fields found for FK data: {fk_data}"
                    )
                )
                return None

            fk_record = model.objects.filter(**filter_kwargs).values("id").first()

            if fk_record:
                return fk_record["id"]
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"WARNING: No matching {model.__name__} found with filters: {filter_kwargs}"
                    )
                )
                return None

        except Exception as e:
            logger.exception(str(e))
            self.stdout.write(self.style.ERROR(f"ERROR resolving FK: {str(e)}"))
            return None

    def resolve_m2m_ids(self, model_info, m2m_data_list):
        """
        Resolve multiple foreign key IDs for ManyToMany relationships
        """
        ids = []
        for fk_data in m2m_data_list:
            fk_id = self.resolve_foreign_key_id(model_info, fk_data)
            if fk_id:
                ids.append(fk_id)
        return ids

    def create_objects_with_m2m(
            self,
            model,
            data_list,
            unique_fields,
            fk_model_dict,
            m2m_fields,
            name,
            app_label,
            app_model,
    ):
        """Create objects and handle ManyToMany relationships separately"""
        try:
            with transaction.atomic():
                user_instance = CustomUser.objects.filter(
                    is_active=True, is_superuser=True
                ).first()

                supports_created_by = "created_by" in [
                    f.name for f in model._meta.get_fields()
                ]

                created_objects = []
                skipped_count = 0

                for data in data_list:
                    # Separate M2M data from regular data
                    m2m_data = {}
                    regular_data = {}

                    for field_name, value in data.items():
                        if field_name in m2m_fields:
                            m2m_data[field_name] = value
                        else:
                            regular_data[field_name] = value

                    # Handle foreign key relationships for regular fields
                    for field_name, model_info in fk_model_dict.items():
                        if field_name in regular_data and field_name not in m2m_fields:
                            fk_data = regular_data[field_name]
                            fk_id = self.resolve_foreign_key_id(model_info, fk_data)

                            if fk_id:
                                regular_data[field_name + "_id"] = fk_id
                                regular_data.pop(field_name)
                            else:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"WARNING: No matching {model_info['model'].__name__} found for {field_name}={fk_data}. Skipping this record."
                                    )
                                )
                                continue

                    # ---- GET OR CREATE WITH UPDATE ----
                    # Prepare lookup kwargs for get_or_create
                    if unique_fields:
                        lookup_kwargs = {field: regular_data[field] for field in unique_fields if field in regular_data}
                        defaults = {k: v for k, v in regular_data.items() if k not in unique_fields}
                    else:
                        lookup_kwargs = regular_data.copy()
                        defaults = {}

                    # Add created_by to defaults if supported and not in lookup_kwargs
                    if supports_created_by and 'created_by' not in lookup_kwargs:
                        defaults['created_by'] = user_instance

                    created = False
                    obj = None

                    try:
                        obj, created = model.objects.get_or_create(
                            **lookup_kwargs,
                            defaults=defaults
                        )

                        if not created:
                            # Object exists, update non-unique fields
                            updated = False

                            # Update all fields that are not in lookup_kwargs
                            for field_name, value in regular_data.items():
                                if field_name not in lookup_kwargs:
                                    current_val = getattr(obj, field_name, None)
                                    if str(current_val) != str(value):  # Convert to string to avoid Decimal vs string comparison issues
                                        setattr(obj, field_name, value)
                                        updated = True

                            # Set created_by if it's empty and we support it
                            if supports_created_by and not obj.created_by_id:
                                obj.created_by = user_instance
                                updated = True

                            if updated:
                                obj.save()

                    except Exception:
                        # Fallback query verification if get_or_create fails due to DB unique constraints
                        try:
                            obj = model.objects.filter(**lookup_kwargs).first()

                            if obj:
                                created = False
                                updated = False

                                for field_name, value in regular_data.items():
                                    if field_name not in lookup_kwargs:
                                        current_val = getattr(obj, field_name, None)
                                        if str(current_val) != str(value):
                                            setattr(obj, field_name, value)
                                            updated = True

                                if supports_created_by and not obj.created_by_id:
                                    obj.created_by = user_instance
                                    updated = True

                                if updated:
                                    obj.save()
                            else:
                                possible_lookups = []

                                for field in model._meta.get_fields():
                                    if hasattr(field, 'unique') and field.unique and field.name in regular_data:
                                        possible_lookups.append({field.name: regular_data[field.name]})

                                for lookup in possible_lookups:
                                    obj = model.objects.filter(**lookup).first()
                                    if obj:
                                        break

                                if obj:
                                    created = False
                                    updated = False

                                    for field_name, value in regular_data.items():
                                        current_val = getattr(obj, field_name, None)
                                        if str(current_val) != str(value):
                                            setattr(obj, field_name, value)
                                            updated = True

                                    if supports_created_by and not obj.created_by_id:
                                        obj.created_by = user_instance
                                        updated = True

                                    if updated:
                                        obj.save()
                                else:
                                    continue

                        except Exception as fallback_error:
                            logger.exception(str(fallback_error))
                            continue

                    if obj is None:
                        continue

                    # Track statistics
                    if not created:
                        skipped_count += 1

                    # Handle ManyToMany relationships
                    for field_name, value in m2m_data.items():
                        if value:
                            m2m_manager = getattr(obj, field_name)

                            if field_name in fk_model_dict:
                                model_info = fk_model_dict[field_name]

                                if isinstance(value, list):
                                    ids = self.resolve_m2m_ids(model_info, value)
                                    if ids:
                                        m2m_manager.set(ids)
                                else:
                                    fk_id = self.resolve_foreign_key_id(model_info, value)
                                    if fk_id:
                                        m2m_manager.set([fk_id])
                            else:
                                if isinstance(value, list):
                                    m2m_manager.set(value)
                                else:
                                    m2m_manager.set([value])

                    if created:
                        created_objects.append(obj)

                updated_count = skipped_count - len([obj for obj in created_objects if obj])

                if created_objects:
                    self.is_dump_data[name] = True
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"SUCCESS: {len(created_objects)} objects created, {updated_count} updated, {skipped_count - updated_count} skipped "
                            f"for '{name}': '{app_label}_{app_model}'"
                        )
                    )
                else:
                    if skipped_count > 0:
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"SUCCESS: 0 objects created, {updated_count} updated, {skipped_count - updated_count} skipped "
                                f"for '{name}': '{app_label}_{app_model}'"
                            )
                        )
                    else:
                        self.stdout.write(
                            self.style.WARNING(
                                f"WARNING: No objects created for '{name}': '{app_label}_{app_model}'"
                            )
                        )

        except Exception as e:
            logger.exception(str(e))
            self.stdout.write(self.style.ERROR(f"ERROR: {str(e)}"))
