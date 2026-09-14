# Idempotent Database Seeding Utility (`load_data`) Developer Guide

This guide explains the design, JSON configuration structure, and operation of the custom **`load_data`** database seeding engine built for the Voicebot backend.

Unlike Django's default `loaddata` command, the `load_data` utility is:
1. **Fully Idempotent**: It will never create duplicate rows or crash on unique database constraint conflicts. Instead, it locates existing matching records, compares fields, updates any changes, and skips matching rows.
2. **Abstract Relation Lookup**: You do not need to hardcode arbitrary primary key IDs (UUIDs or Integers) in your JSON fixtures. Instead, you specify descriptive query fields to dynamically find related database items (Foreign Keys and Many-to-Many targets).

---

## 📂 Directory Architecture

The seeding system is centralized under two root-level directories:

* **JSON Fixtures Location**: `/json_files/`
* **Seeding Command & Documentation**: `/user/management/commands/`
  * `load_data.py` (Command Implementation)
  * `load_data_guide.md` (This Guide)

---

## 🛠️ Seeding Command Usage

Run the seeding command via Django's command line interface:

### 1. Seeding a Specific Model Fixture
Loads a single configuration file matching the file name:
```bash
# Form: python manage.py load_data {file_name}
uv run python manage.py load_data language --settings=config.settings.local
```

> **Plans are not seeded.** Plans, their rate cards and their entitlements are created only
> from the super admin panel — there is no `plan` fixture to load.

### 2. Seeding All Fixtures at Once
Loads the index configuration file `json_files/all.json` and runs all declared configs sequentially:
```bash
uv run python manage.py load_data all --settings=config.settings.local
```

### 3. Execution inside Docker Containers
To seed reference data inside a running Docker Daphne container, invoke:
```bash
docker compose exec backend python manage.py load_data all
```

---

## 📝 Configuration File Structures

All seed JSON files are placed inside the `/json_files/` directory.

### 1. The Master Index (`json_files/all.json`)
Declares the order in which data models should be seeded (crucial for maintaining database referential integrity):
```json
{
  "all_file_name": [
    "timezone",
    "language"
  ]
}
```

### 2. A Core Seed File (`json_files/language.json`)
Below is an annotated definition of a seed configuration file illustrating field syntax:
```json
{
  "app_label": "onboard",
  "model": "language",
  "unique_fields": [
    "code"
  ],
  "data": [
    {
      "name": "English (US)",
      "code": "en-US"
    }
  ]
}
```

* **`unique_fields`**: The fields that identify an existing row. A row matching them is updated in place rather than duplicated, which is what makes re-running the command safe.
* **`data`**: Every key other than a relation is passed straight to the model, so each key must be a real field on it — a stale key fails the whole file.

---

## 🔗 Handling Database Relationships

To maintain abstraction and avoid environment-specific UUIDs, you can dynamically resolve foreign relations inside the JSON specification.

### 1. Foreign Key Configuration (`fk`)
To resolve a Foreign Key relationship on a model record, define an array under the `"fk"` property block mapping the field to lookup criteria:

```json
{
  "app_label": "your_app",
  "model": "your_model",
  "unique_fields": ["code"],
  "fk": [
    {
      "field": "owner",
      "app_label": "user",
      "model": "customuser",
      "lookup_fields": ["email"]
    }
  ],
  "data": [
    {
      "code": "record_01",
      "owner": {
        "email": "admin@example.com"
      }
    }
  ]
}
```

* **`field`**: The name of the foreign key attribute on your model (e.g. `"owner"` will resolve to the database field `"owner_id"`).
* **`lookup_fields`**: The query parameters used to look up the matching related record. The engine will match and retrieve the record ID automatically.

### 2. Many-to-Many Configuration (`m2m`)
Many-to-Many relationships are handled exactly like Foreign Keys, but support lists of lookup dictionaries:

```json
{
  "app_label": "your_app",
  "model": "your_model",
  "unique_fields": ["code"],
  "fk": [
    {
      "field": "permissions",
      "app_label": "auth",
      "model": "permission",
      "lookup_fields": ["codename", "content_type__app_label"]
    }
  ],
  "data": [
    {
      "code": "role_01",
      "permissions": [
        {
          "codename": "add_plan",
          "content_type__app_label": "onboard"
        },
        {
          "codename": "change_plan",
          "content_type__app_label": "onboard"
        }
      ]
    }
  ]
}
```

---

## 🔒 Transaction Safety & Failure Isolation
* **Transactional Enclosure**: The loading of every file is executed within a `transaction.atomic()` block. If any single data record fails validation, the entire file import is completely rolled back, keeping the database in a consistent state.
* **Audit Fields (`created_by`)**: The engine automatically detects audit configurations. If the model includes a `created_by` relationship, it automatically retrieves the active superuser account to assign as the creator of newly seeded records.
