# Agent Call Log API

The Agent Call Log API provides endpoints to store and retrieve conversation details (call logs) associated with Voicebot Agents.

**Base URL**: `/api/v1/core/agent-call-logs/`

## General Rules
- **Authentication**: `Bearer <token>` (JWT or VB_AGENT token)
- **Tenant Scoping**: All records are isolated per tenant. Users will only see logs belonging to their organization.
- **Creation Restrictions**: The `POST` (create) method is strictly restricted to `VB_AGENT` tokens. Standard users cannot manually create call logs.

---

## 1. List Call Logs
Retrieve a paginated list of call logs under your tenant.

**Endpoint:** `GET /api/v1/core/agent-call-logs/`
**Required Permissions:** `IsAuthenticated`

**Available Filters (Query Params):**
- `?direction=inbound|outbound`
- `?outcome=order_placed`
- `?agent=<agent-uuid>`
- `?search=<provider_call_id or caller_number>`
- `?ordering=-started_at`

**Response (200 OK):**
```json
{
  "status": true,
  "message": "Listed successfully",
  "data": [
    {
      "id": "uuid-string",
      "provider_call_id": "call-001",
      "agent": {
        "id": "agent-uuid",
        "bot_name": "KisanMitra"
      },
      "caller_number": "+91 93880 25735",
      "direction": "inbound",
      "duration_seconds": 326,
      "outcome": "order_placed",
      "started_at": "2026-06-23T17:33:31.671Z"
    }
  ]
}
```

---

## 2. Retrieve Call Log Details
Fetch the full transcript and metadata for a specific call.

**Endpoint:** `GET /api/v1/core/agent-call-logs/<uuid:id>/`
**Required Permissions:** `IsAuthenticated`

**Response (200 OK):**
```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "id": "uuid-string",
    "provider_call_id": "call-001",
    "agent": {
      "id": "agent-uuid",
      "bot_name": "KisanMitra"
    },
    "caller_number": "+91 93880 25735",
    "caller_name": "Nisha Patel",
    "direction": "inbound",
    "campaign_id": null,
    "duration_seconds": 326,
    "outcome": "order_placed",
    "mood": "neutral",
    "mood_confidence": 0.85,
    "pii_redacted": false,
    "ai_summary": "Prospect interested in 2BHK property...",
    "preferred_language": "Spanish",
    "probing_questions_count": 3,
    "probing_questions": [
      {
        "question": "What is your monthly estimated call volume?",
        "answerSummary": "Over 50,000 calls per month.",
        "isPositive": true
      }
    ],
    "follow_up_detail": "Follow-up with alternative plans after contract review.",
    "human_transfer_detail": null,
    "transcript": [
      {
        "id": "tl-0",
        "speaker": "bot",
        "text": "Hello! Thank you for calling.",
        "timestamp": 0,
        "isRedacted": false
      }
    ],
    "recording_url": "/recordings/call-1.mp3",
    "started_at": "2026-06-23T17:33:31.671Z",
    "ended_at": "2026-06-23T17:38:57.671Z",
    "tags": ["urgent"]
  }
}
```

---

## 3. Create Call Log (VB_AGENT Only)
Ingest a new conversation log from the telephony provider.

**Endpoint:** `POST /api/v1/core/agent-call-logs/`
**Required Permissions:** `IsVoicebotAgent` (Standard Users/JWT will receive a 403 Forbidden)

**Request Payload:**
```json
{
    "provider_call_id": "call-001",
    "agent": "agent-uuid-string",
    "caller_number": "+91 93880 25735",
    "caller_name": "Nisha Patel",
    "direction": "inbound",
    "duration_seconds": 326,
    "outcome": "order_placed",
    "mood": "neutral",
    "mood_confidence": 0.85,
    "pii_redacted": false,
    "ai_summary": "Prospect interested in 2BHK property.",
    "preferred_language": "Spanish",
    "probing_questions_count": 3,
    "probing_questions": [
        {
            "question": "What is your monthly estimated call volume?",
            "answerSummary": "Over 50,000 calls per month.",
            "isPositive": true
        }
    ],
    "follow_up_detail": "Follow-up with alternative plans after contract review.",
    "human_transfer_detail": null,
    "transcript": [
        {
            "id": "tl-0",
            "speaker": "bot",
            "text": "Hello! Thank you for calling.",
            "timestamp": 0,
            "isRedacted": false
        }
    ],
    "recording_url": "/recordings/call-1.mp3",
    "started_at": "2026-06-23T17:33:31.671Z",
    "ended_at": "2026-06-23T17:38:57.671Z",
    "tags": ["urgent"]
}
```

**Response (201 Created):**
```json
{
  "status": true,
  "message": "Created successfully",
  "data": {
    "id": "new-uuid-string",
    "provider_call_id": "call-001",
    "...": "..."
  }
}
```

**Error Responses:**
- `403 Forbidden`: `{"status": false, "message": "Permission denied."}` (If caller is not a VB_AGENT).
- `400 Bad Request`: `{"status": false, "errors": {"provider_call_id": ["This field is required."]}}`
