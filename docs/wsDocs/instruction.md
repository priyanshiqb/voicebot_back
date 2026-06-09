# WebSocket Architecture & Maintenance Guide

Welcome to the WebSocket documentation (`wsDocs`). This directory serves as the single source of truth for all real-time communication modules, consumer architectures, message protocols, and error definitions.

---

## 1. Module Lifecycle: Plan, Update, Migrate, Delete

### Planning a New WebSocket Module
When designing a new WebSocket feature, follow these steps:
1. **Determine Scope**: Decide if the connection is global (`BaseConsumer`) or restricted to a specific tenant (`TenantBaseConsumer`).
2. **Define the Group**: Create a configuration in `utils/websocket/group_registry.py`. Define the `prefix` and the required `context` keys (e.g., `["tenant_id", "user_id"]`).
3. **Draft the Consumer**: Create a new file in `utils/websocket/consumers/` (or the respective app directory). Inherit from the appropriate base consumer.
4. **Document**: Before writing business logic, define the expected input events and output payloads in `wsDocs/`.

### Updating & Migrating Existing Modules
1. **Backwards Compatibility**: When adding new fields to an outgoing event, ensure old clients won't break. 
2. **Event Versioning**: If an event payload changes drastically, create a new `event_type` (e.g., `chat.message_v2`) rather than breaking the existing `chat.message`.
3. **Graceful Degradation**: If migrating channels, ensure the old consumer routes can gracefully redirect or inform the client to upgrade.

### Deleting / Sunsetting Modules
1. **Deprecation Notice**: Send a deprecation warning payload to connected clients before fully shutting down a module.
2. **Route Removal**: Remove the routing from `urls/websocket_routing.py`.
3. **Clean Up**: Delete the consumer class, remove its group registry definition, and archive its documentation in `wsDocs/`.

---

## 2. Actively Maintaining Documentation

* **Mandatory Updates**: Any change to a consumer's input arguments, output payloads, or group routing **must** be reflected in this directory in the same Pull Request/Task.
* **File Structure**: 
  * Keep this `instruction.md` as the core guideline.
  * Create specific markdown files for major features (e.g., `chat_module.md`, `notifications.md`).
* **Format**: Use JSON code blocks to illustrate payload examples. Clearly list required vs. optional fields.

---

## 3. Standard Message Protocol (Input/Output)

All WebSocket consumers must adhere to a standardized JSON payload structure to ensure consistent frontend parsing.

### Standard Output (Server to Client)
When sending messages from the server, always use the `send_update` method provided by the Base Consumers.

```json
{
    "type": "event_type_string",  // e.g., "connection.established", "chat.new_message"
    "message": "Human readable description or status message", // Optional
    "data": {                     // Optional payload body
        "id": "123",
        "content": "Hello World"
    }
}
```

### Expected Input (Client to Server)
When the client sends an action to the server, it should match the following format:

```json
{
    "action": "action_name",      // e.g., "send_message", "join_room"
    "data": {                     // The payload associated with the action
        "text": "Hello",
        "room_id": "xyz"
    }
}
```

---

## 4. WebSocket Error Codes & Connection Closures

We use custom WebSocket close codes (ranging from `4000` upwards) to help the frontend gracefully handle disconnections.

### Common Global Errors (`core/constants/ws_errors.py`)
These are universally handled by the Base Consumers:

| Code | Constant | Reason | Resolution |
| :--- | :--- | :--- | :--- |
| **4000** | `MISSING_TENANT_ID` | The URL route is missing the `tenant_id` kwarg. | Ensure frontend connects to `/ws/tenant/<id>/...` |
| **4001** | `INVALID_TENANT` | The `tenant_id` does not exist in the database. | Refresh context or select a valid tenant. |
| **4002** | `GROUP_REGISTRY_NOT_DEFINED` | Consumer missing `group_registry` config. | Backend Developer: Assign `group_registry`. |
| **4003** | `UNAUTHENTICATED` | Missing, expired, or invalid JWT token. | Client must refresh token and reconnect. |
| **4004** | `INTERNAL_ERROR` | Group building or unexpected backend crash. | Review backend logs. |

### Creating Module-Specific Errors
To maintain a single source of truth and prevent error code collisions across different modules, **all** WebSocket closing errors (global or module-specific) must be defined centrally in `core/constants/ws_errors.py`.

1. Add your module-specific error constant to the `WSErrors` class.
2. Group the errors logically within the class.
3. Ensure the error code is strictly unique across the entire file (e.g., using `4100` ranges for Chat, `4200` ranges for Calls).
4. Update this `instruction.md` document or the specific module `.md` document in `wsDocs/` whenever a new error is added.

**Example:**
```python
# core/constants/ws_errors.py
class WSErrors:
    # ... Global Errors ...
    
    # --- Chat Module Errors ---
    CHAT_BANNED_FROM_ROOM = (4100, "You have been banned from this room.")
    CHAT_ROOM_FULL = (4101, "This room has reached maximum capacity.")
```

---

## 5. Async Strictness & Performance Rules

Django Channels is strictly built on the Python `asyncio` event loop. To prevent server freezes or WebSocket connection drops, you must enforce strict asynchronous programming.

### Prevent "Too Long to Close" Errors
If synchronous, blocking code (like a slow API call, file reads, or direct database queries) is placed inside an async consumer, it completely freezes the event loop. This leads to the server being unable to cleanly close or accept sockets, resulting in the dreaded Channels error: 

> `Application instance <Task pending...> took too long to shut down and was killed.`

* **Rule 1 - `async def` Everywhere**: Always define consumer methods using `async def` and ensure you use `await` when calling them. Do not write standard `def` methods inside a consumer unless they execute in microseconds and contain zero I/O operations.
* **Rule 2 - Database Isolation**: Never execute standard Django ORM queries (e.g., `User.objects.get()`) directly inside a consumer. You **must** wrap all database logic inside a separate method decorated with `@database_sync_to_async` and `await` that method from your main flow.
