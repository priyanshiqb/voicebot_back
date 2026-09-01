# Users API Documentation

The people who can sign in to your workspace. Create colleagues, edit profiles, change passwords,
and read the current user's own record.

**Base path:** `/api/v1/auth/users/`

Four things govern this API:

- **Your visible set is your tenant.** You can see — and **edit** — every user mapped to the same
  tenant as you, plus your own record. Superusers see everyone. See
  [Who can see and change whom](#who-can-see-and-change-whom); it is broader than most people
  expect.
- **Creating a user maps them to your tenant automatically.** You do not pass a tenant; it comes
  from your own mapping. Without a mapping, creation is refused.
- **Roles are not settable here.** `user_type` is absent from the serializer, so every user created
  through this API is an ordinary `USER`. Roles are an admin-side concern.
- **`GET /users/me/` is the endpoint your app should call on load** — it returns the caller's
  record along with their tenant and subscription.

> **VB_AGENT access:** none. This viewset declares no `vb_agent_allowed_actions`, so agent tokens
> get **403** on every action.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/auth/users/` | List users you can see (paginated) |
| `GET` | `/auth/users/{id}/` | Retrieve one user |
| `POST` | `/auth/users/` | Create a user in your tenant |
| `PUT` / `PATCH` | `/auth/users/{id}/` | Update a user |
| `DELETE` | `/auth/users/{id}/` | Soft-delete a user |
| `GET` | `/auth/users/me/` | The authenticated user's own record |
| `POST` | `/auth/users/change-password/` | Change your own password |
| `GET` | `/auth/users/dropdown/` | Label/value picker options |
| `POST` | `/auth/users/bulk_delete/` | Bulk soft-delete |
| `POST` | `/auth/users/{id}/set-role/` | Promote or demote between `ADMIN` and `MEMBER` |
| `POST` | `/auth/users/{id}/transfer-ownership/` | Hand `OWNER` to somebody else |

---

## Who can see and change whom

**Visibility and edit rights are no longer the same thing.** Everyone at a tenant can *see* their
colleagues; what they may *do* to them comes from `TenantUserMapping.role`.

| Caller | Visible set |
|---|---|
| Django superuser | **Every user in every tenant.** |
| User with a tenant mapping | Their own record **plus every user mapped to the same tenant**. |
| User without a tenant mapping | Their own record only. |

### Roles

Set per tenant on the membership, not on the user account. `CustomUser.user_type` is a different
thing entirely — it marks machine and platform accounts and is never a tenant role.

| Role | May do |
|---|---|
| `OWNER` | Everything an admin can, plus transfer ownership. **Exactly one per tenant**, enforced by a database constraint. Cannot be edited, removed, or demoted by anybody else. |
| `ADMIN` | Create, edit, remove, and re-role other people. **Cannot touch the owner.** |
| `MEMBER` | Edit only their own record. The default for a newly created or invited account. |

| Action | `MEMBER` | `ADMIN` | `OWNER` |
|---|---|---|---|
| List / retrieve colleagues | ✅ | ✅ | ✅ |
| Edit **own** record | ✅ | ✅ | ✅ |
| Edit / delete a **member or admin** | ❌ 403 | ✅ | ✅ |
| Edit / delete the **owner** | ❌ 403 | ❌ 403 | ✅ (self) |
| Create a user · invite · bulk delete | ❌ 403 | ✅ | ✅ |
| `set-role` | ❌ 403 | ✅ | ✅ |
| `transfer-ownership` | ❌ 403 | ❌ 403 | ✅ |

> **The owner is deliberately untouchable by others.** Without that, two admins can remove each
> other, or remove whoever holds the billing relationship, leaving a tenant that cannot appoint
> anyone. Ownership moves only by transfer, which demotes the outgoing owner to `ADMIN` in the same
> transaction so there is never a moment with two owners or none.

> **A password can never be set through this resource.** See §4.

> **Soft-deleting or deactivating a user does not sign them out.** Their existing sessions keep
> working until the tokens expire — revoke them via [Sessions](./sessions.md) as a separate step.

---

## Endpoint Details

### 1. List Users

- **Path**: `GET /api/v1/auth/users/`
- **Headers**: `Authorization: Bearer <access_token>`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`. |
| `search` | string | Partial, case-insensitive match on `email`, `username`, `first_name`, `last_name`, `phone_number`. | Words split on spaces and **AND**ed. |
| `ordering` | string | Sort. Default `-created_at`. | Only `created_at`, `updated_at`, `email`, `first_name`, `last_name`, optionally `-` prefixed. |
| `is_active` | boolean | Exact match. | `true` / `false`. |
| `is_verified` | boolean | Exact match. | `true` / `false`. |
| `created_at__date` | date | Users created on an exact date. | `YYYY-MM-DD`. |
| `created_at__gte` / `created_at__lte` | datetime | Created on or after / before. | ISO-8601. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
        "email": "alex@acme.com",
        "first_name": "Alex",
        "last_name": "P",
        "phone_number": "+14155552671",
        "is_active": true,
        "is_verified": true,
        "session_timeout_hours": 168,
        "created_at": "2026-06-03T18:00:00Z",
        "timezone": { "id": "3fa8…", "timezone": "Asia/Kolkata" },
        "language": { "id": "7bf2…", "name": "English" }
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

> **The list payload omits `tenant` and `subscription`** — both require extra queries per row. Use
> retrieve (§2) or `me` (§6) when you need them.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more users. Always at least your own record. | `success` |
| **400** | Non-boolean on a boolean filter, or a malformed date. | filter error |
| **401** | Missing, expired, or revoked access token. | `Authentication credentials were not provided.` |
| **403** | Agent token. | `This endpoint is not available to agent tokens.` |
| **404** | `page` beyond the last page. | `Invalid page.` |

---

### 2. Retrieve a User

- **Path**: `GET /api/v1/auth/users/{id}/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Retrieved successfully",
    "data": {
      "id": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
      "email": "alex@acme.com",
      "first_name": "Alex",
      "last_name": "P",
      "phone_number": "+14155552671",
      "is_active": true,
      "is_verified": true,
      "session_timeout_hours": 168,
      "tenant": { "id": "7bf2…", "name": "Alpha Corp", "subdomain": "alpha-corp" },
      "subscription": { "id": "9c7e…", "status": "active", "plan": { "id": "1a2b…", "name": "Growth" } },
      "timezone": { "id": "3fa8…", "timezone": "Asia/Kolkata" },
      "language": { "id": "7bf2…", "name": "English" }
    },
    "errors": null
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Found. | `Retrieved successfully` |
| **401** / **403** | As in §1. | — |
| **404** | No such user, soft-deleted, or outside your visible set. | `Not found.` |

> **The detail payload has no timestamps.** Unlike the list, `created_at` and `updated_at` are not
> serialised here. If you need them, read them from the list.

> **`tenant` and `subscription` are `null` for a user with no tenant mapping.** `subscription`
> prefers the tenant's `active` subscription and falls back to whichever exists, so a `null` here
> means the tenant has none at all.

---

### 3. Create a User

Adds a colleague to **your** tenant.

- **Path**: `POST /api/v1/auth/users/`
- **Request Payload**:
  ```json
  {
    "email": "sam@acme.com",
    "password": "Str0ng!Passphrase",
    "first_name": "Sam",
    "last_name": "Rivera",
    "phone_number": "+14155559876"
  }
  ```

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `email` * | string | Sign-in identity. Also becomes `username` if none is set. | Required, valid email, **unique across all users**. Lower-cased on save. |
| `password` | string | Initial password. **Write-only** — never returned. | Optional. Validated by Django's configured password validators (length, common-password, numeric-only, similarity to the email). **Not** the same rules as [change-password](#7-change-your-password). Omitting it creates an account that cannot log in until a password is set. |
| `first_name` | string | Given name. | Optional. Max **150**. |
| `last_name` | string | Family name. | Optional. Max **150**. |
| `phone_number` | string | Contact number. Free text — **not** normalised or validated. | Optional, nullable. Max **15**. |
| `is_active` | boolean | Whether the account may sign in. | Optional, default `true`. |
| `session_timeout_hours` | integer | How long this user's refresh tokens live. See [auth](./auth.md#token-lifetimes). | Optional, default `168` (7 days). Values below 1 are clamped to 1 hour at token-issue time. |
| `timezone` | UUID | Preferred timezone, for schedules and notifications. **Changing it also moves this user's [availability profile](../core/availability_profile.md#the-profile-timezone-follows-the-hosts-when-the-two-were-in-step) onto the new zone**, if they have one and it was in step with their old timezone. | Optional, nullable. Must be an existing [timezone](../onboard/timezones.md) id. |
| `language` | UUID | Preferred language. | Optional, nullable. Must be an existing [language](../onboard/languages.md) id. |

**Read-only — ignored if you send them:** `id`, `is_verified`, `tenant`, `subscription`.

> **Only an owner or admin may create a user.** A member gets a **403**, checked *before* the
> plan-limit check so a member is never handed a 402 that discloses how full the plan is.

> **The new user arrives as a `MEMBER`**, whatever the creator's own role. Promotion is a separate,
> deliberate act through [set-role](#10-set-a-users-role), so nobody is created already able to
> remove the person who created them.

> **`user_type` cannot be set.** It is absent from the serializer, so every user created here gets
> the model default `USER`. That field is not a tenant role — see [Roles](#roles).

> **Consider [inviting](./invitations.md) instead.** Creating a user here means *you* choose their
> password and then tell them what it is. An invitation lets them set their own and never shares a
> credential.

> **The new user is mapped to your tenant automatically**, inside the same transaction as the
> account itself — so if the mapping fails, no orphaned user is left behind. Superusers are the
> exception: they create unmapped users.

- **Success Response (201 Created)**: the object from §2, with `"message": "Created successfully"`.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Created and mapped to your tenant. | `Created successfully` | `null` |
| **400** | `email` already taken. | `custom user with this email already exists.` | `{"email": [...]}` |
| **400** | `email` missing or malformed. | `This field is required.` | `{"email": [...]}` |
| **400** | `password` rejected by a Django validator. | e.g. `This password is too common.` | `{"password": [...]}` |
| **400** | `timezone` or `language` id does not exist. | `Invalid pk "…" - object does not exist.` | `{"<field>": [...]}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": …}` |
| **402** | Your plan has no seats left. | `QUOTA_EXCEEDED` | `{"detail": …}` |
| **403** | You are a `MEMBER`. | `Only an owner or admin can manage the people at this organisation.` | `{"detail": …}` |
| **403** | You have no tenant mapping to add the user to. | `Your user account is not associated with any active tenant.` | `{"detail": …}` |
| **403** | Agent token. | `This endpoint is not available to agent tokens.` | `{"detail": …}` |

> **Password rules differ across this API.** Create and update run Django's validators;
> [change-password](#7-change-your-password) and
> [reset-password](./auth.md#7-reset-password-step-3) additionally require an uppercase, a
> lowercase, a digit, a special character, no spaces, and a 8–32 length. A password acceptable here
> may be rejected there.

---

### 4. Update a User

- **Path**: `PUT` (full) or `PATCH` (partial) `/api/v1/auth/users/{id}/`
- **Request Payload**: any subset of the writable fields from §3.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Updated. | `Updated successfully` | `null` |
| **400** | Any validation error from §3, including the new email colliding. | as §3 | `{"<field>": [...]}` |
| **400** | `password` was included. | `A password cannot be set here. Use /users/change-password/ to change your own password.` | `{"password": [...]}` |
| **403** | You may not modify this user — wrong role, or the target is the owner. | `You do not have permission to modify this user.` | `{"detail": …}` |
| **401** / **403** | As in §1. | — | — |
| **404** | No such user, or outside your visible set. | `Not found.` | `{"detail": …}` |

> **Sending `password` here is refused with a 400.** It used to change the password with no
> old-password check, which — combined with every colleague being visible — meant any user could
> overwrite a teammate's credentials and sign in as them. Two ways to set a password remain, and
> both prove something first: [change-password](#7-change-your-password) verifies the current one,
> and the [invitation](./invitations.md) and [reset](./auth.md) flows prove control of the mailbox.
>
> The rejection is explicit rather than a silent drop, so a client cannot believe it changed a
> password that it did not.

> **Editing anybody but yourself requires `OWNER` or `ADMIN`**, and nobody but the owner may edit
> the owner. `is_active` is covered by this too — deactivating a colleague is a manager action.

> **Changing `email` changes the user's login identity immediately**, and their existing sessions
> keep working. It does not send a verification email; `is_verified` is untouched.

---

### 5. Delete a User

- **Path**: `DELETE /api/v1/auth/users/{id}/`
- **Success Response (200 OK)**: `{ "status": true, "message": "Deleted successfully", "data": null, "errors": null }`

| Status | When | `message` |
|---|---|---|
| **200** | Soft-deleted, and the seat released. | `Deleted successfully` |
| **403** | You are a `MEMBER`, or the target is the owner. | `You do not have permission to modify this user.` |
| **401** | Missing or invalid token. | — |
| **404** | No such user, already deleted, or outside your visible set. | `Not found.` |

**Soft delete** — the row survives with `is_deleted: true` and drops out of every list.

> **Their sessions are NOT revoked.** A deleted user's access token keeps working until it expires,
> though [refresh](./auth.md#3-refresh) refuses with `User account is deleted.` To sign them out
> now, revoke their sessions as well.

> **The email stays taken.** Uniqueness on `email` has no "not deleted" condition, so a
> soft-deleted user's address cannot be reused for a new account.

> **Deleting anybody but yourself requires `OWNER` or `ADMIN`, and the owner cannot be deleted
> by anyone else.** A 403 covers both cases with the same message, so it cannot be used to work out
> who the owner is.

> **The seat goes back to your plan.** The `USER` allowance counts live tenant memberships, so the
> membership is soft-deleted in the same transaction as the user. Without that the seat would stay
> consumed forever — the `OneToOneField` cascade never fires here, because a soft delete is a flag
> write rather than a delete.

> **Nothing stops you deleting yourself**, including as the owner. There is no self-protection
> check, and a tenant whose owner deletes themselves is left with no owner until one is appointed
> out of band.

---

### 6. Get the Current User

The endpoint your app calls on load.

- **Path**: `GET /api/v1/auth/users/me/`
- **Success Response (200 OK)**: the same object as §2, for the authenticated caller.

| Status | When | `message` |
|---|---|---|
| **200** | Always, for any authenticated user. | `success` |
| **401** | Missing, expired, or revoked access token. | `Authentication credentials were not provided.` |
| **403** | Agent token. | `This endpoint is not available to agent tokens.` |

> **Never 404s.** It serialises the request's own user directly rather than looking one up, so it
> works even for a user with no tenant mapping — `tenant` and `subscription` come back `null`.

---

### 7. Change Your Password

Self-service change, verifying the current password.

- **Path**: `POST /api/v1/auth/users/change-password/`
- **Applies to the caller only** — there is no `id`; you cannot change someone else's password here.

| Field | Type | Description | Validation |
|---|---|---|---|
| `old_password` * | string | Your current password. | Required. Must match. |
| `new_password` * | string | The replacement. | Required. **8–32 characters**, no spaces, at least one uppercase, one lowercase, one digit, and one of `!@#$%^&*(),.?":{}\|<>`. Also run through Django's validators. Must differ from `old_password`. |
| `confirm_password` * | string | Repeat of `new_password`. | Required. Must match exactly. |

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Password changed. | `Password changed successfully.` | `null` |
| **400** | `old_password` is wrong. | `Old password is incorrect.` | `{"old_password": "…"}` |
| **400** | `new_password` and `confirm_password` differ. | `New passwords do not match.` | `{"confirm_password": "…"}` |
| **400** | The new password equals the old one. | `New password cannot be the same as old password.` | `{"new_password": "…"}` |
| **400** | A complexity rule failed. | e.g. `Password must contain at least one special character.` | `{"new_password": [...]}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": …}` |

> **Changing your password does not sign out your other devices.** Sessions survive — revoke them
> from [Sessions](./sessions.md) if that is what you intend.

---

### 8. Dropdown

- **Path**: `GET /api/v1/auth/users/dropdown/`
- **Query Parameters**: `limit` (≤ **1000**; over that → **400 `Limit cannot exceed 1000`**), plus all List filters and `search`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [ { "label": "Alex P", "value": "5fa85f64-…" } ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

`label` falls back in order: `"First Last"` → `First` → `username` → `email`. So a user with no
name still renders as something identifiable.

---

### 9. Bulk Delete

- **Path**: `POST /api/v1/auth/users/bulk_delete/`
- **Request Payload**: `{ "ids": ["5fa85f64-…", "…"] }`

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Users to soft-delete. | Non-empty list. Every id must exist, be undeleted, and be in your visible set. All-or-nothing. |

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown, already deleted, or outside your visible set — **nothing is deleted**. | `Some ids are invalid` |
| **403** | You are a `MEMBER`, or any id in the batch is one you may not remove — **nothing is deleted**. | `You do not have permission to modify this user.` |
| **401** | Missing or invalid token. | — |

> **Duplicate ids fail the whole request.** De-duplicate client-side.

> **Requires `OWNER` or `ADMIN`.** Every id is permission-checked *before* anything is deleted,
> so a batch containing one user you may not remove — the owner, say — removes **nobody**. That is
> deliberate: a partial success would tell you which single id was protected.

> **Seats are released for every user removed**, exactly as in §5.

> **No session revocation here either**, and no guard against including yourself.

---

### 10. Set a User's Role

Promote or demote somebody between `ADMIN` and `MEMBER`.

- **Path**: `POST /api/v1/auth/users/{id}/set-role/`
- **Request Payload**: `{ "role": "ADMIN" }`

| Field | Type | Description | Validation |
|---|---|---|---|
| `role` * | enum | The new role. | Required. `ADMIN` or `MEMBER` only. |

| Status | When | `message` |
|---|---|---|
| **200** | Role updated. Returns the user object from §2. | `Role updated` |
| **400** | `role` is `OWNER` or not a valid choice. | `"OWNER" is not a valid choice.` |
| **403** | You are a `MEMBER`, or the target is the owner. | `Only an owner or admin can manage the people at this organisation.` |
| **404** | Outside your visible set. | `Not found.` |

> **`OWNER` cannot be granted here.** Use [transfer-ownership](#11-transfer-ownership), which moves
> the role rather than duplicating it. Only one live owner per tenant exists, and it is a database
> constraint rather than a convention — granting a second would fail at the database anyway.

> **A member cannot promote themselves.** Acting on your own record is normally allowed, so this
> endpoint additionally requires that you already be a manager.

---

### 11. Transfer Ownership

- **Path**: `POST /api/v1/auth/users/{id}/transfer-ownership/`
- **Request Payload**: none.

| Status | When | `message` |
|---|---|---|
| **200** | Ownership moved. Returns the new owner. | `Ownership transferred` |
| **403** | You are not the current owner, or the target belongs to another organisation. | `Only the current owner can transfer ownership.` |
| **404** | Outside your visible set. | `Not found.` |

> **The outgoing owner becomes an `ADMIN`**, not a member — they were running the account a moment
> ago, and dropping them to the lowest role is a surprise nobody asked for.

> **Both writes happen in one locked transaction**, so there is never a moment where a tenant has
> two owners or none, even under two simultaneous transfers.

---

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **No session revocation on deactivate, delete, or password change.** | A removed or compromised account keeps working until its access token expires. Revoke via [Sessions](./sessions.md) as a separate step. |
| 2 | **A user's role is not in any response payload.** It is enforced on every write, but a client cannot read it back — so a UI cannot grey out actions it knows will 403. | Clients must attempt the action and handle the 403. |
| 3 | **No last-owner guard.** The owner can delete themselves, leaving the tenant with none. | Recovery needs an out-of-band fix. |
| 4 | **Password rules differ between create/update and change/reset.** | A password accepted at creation may be rejected when the user later tries to change it. |
| 5 | **`phone_number` is unvalidated free text**, unlike [Contacts](../core/contact.md), which normalises to E.164. | The same person's number can be stored in two different shapes across the system. |
| 6 | **A soft-deleted user's email stays reserved.** | The address cannot be reused for a new account. |
| 7 | **The detail payload omits timestamps** that the list includes. | Clients must call the list to learn when a user was created. |

## Related

- [Authentication API](./auth.md) — login, password reset, token lifetimes
- [Invitations API](./invitations.md) — adding people without choosing their password
- [Sessions API](./sessions.md) — revoking access after a change here
- [Timezones](../onboard/timezones.md) · [Languages](../onboard/languages.md) — the `timezone` and `language` ids
