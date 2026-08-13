# 1. Firebase Cloud Messaging (Push Notifications)

**Powers:** internal-team push alerts — a booking reassigned to you, a booking flagged
needs-reschedule. Consumed by [core/services/push.py](../../core/services/push.py).

**What the backend needs:** exactly **one** thing — a **service-account JSON key file**, and its
path in `FIREBASE_CREDENTIALS`. The backend uses it to authenticate as the Firebase project and
send messages. (The *client* apps separately need Firebase client config to produce the device
registration tokens — see the last section; those are **not** backend `.env` values.)

> Blank `FIREBASE_CREDENTIALS` ⇒ push is silently disabled (sends return `0`). Everything else
> still works.

---

## Step by step — obtain the service-account JSON

1. Go to the **[Firebase console](https://console.firebase.google.com/)** → **Add project**
   (or select an existing one). Follow the prompts (Analytics optional).
2. In the project, open **⚙️ Project settings** (gear icon, top-left) → **Cloud Messaging** tab
   → confirm the **Firebase Cloud Messaging API (V1)** is **Enabled**. (If disabled, click
   *Manage API in Google Cloud Console* → Enable.)
3. Go to **Project settings → Service accounts** tab.
4. Click **Generate new private key** → **Generate key**. A JSON file downloads immediately,
   e.g. `myproject-firebase-adminsdk-ab12c-0123456789.json`.
5. **Store it as a secret** on the server (never commit it). Mount it at a stable path, e.g.
   `/run/secrets/firebase-service-account.json`.
6. Put that **path** in `FIREBASE_CREDENTIALS`.

---

## Field-level mapping

| What you get | Where | `.env` variable | Django setting | Consumed by |
|--------------|-------|-----------------|----------------|-------------|
| The **path** to the downloaded JSON key | Service accounts → Generate new private key | `FIREBASE_CREDENTIALS` | `FIREBASE_CREDENTIALS` | [push.py](../../core/services/push.py) `_ensure_app()` → `credentials.Certificate(path)` |

> The value is a **filesystem path to the JSON**, *not* the JSON contents and *not* individual
> fields. `firebase-admin` reads the whole file. Keep the file readable by the app process only.

### `.env`
```dotenv
FIREBASE_CREDENTIALS=/run/secrets/firebase-service-account.json
```

### The JSON's key fields (for verification only — you don't map these individually)
The downloaded file is a standard Google service-account key. You can sanity-check it contains:

| JSON field | Meaning |
|------------|---------|
| `type` | `"service_account"` |
| `project_id` | your Firebase project id |
| `private_key` | the RSA private key (keep secret) |
| `client_email` | `firebase-adminsdk-…@<project>.iam.gserviceaccount.com` |

---

## Client side (frontend / mobile) — for producing device tokens

The backend sends *to* device tokens; your **client apps generate** those tokens using **Firebase
client config** (a different set of values, kept in the frontend, **not** in this backend `.env`).
Included here so the end-to-end picture is clear:

| Client value | Where | Used for |
|--------------|-------|----------|
| `apiKey`, `authDomain`, `projectId`, `messagingSenderId`, `appId` | Project settings → **General → Your apps → SDK setup and configuration** | Initialise the Firebase JS/mobile SDK |
| **Web Push certificate (VAPID key pair)** | Project settings → **Cloud Messaging → Web configuration → Web Push certificates** | Required for **web** push; the browser SDK uses it to obtain a registration token |

The client obtains its FCM registration token, then registers it via
`POST /api/v1/core/device-tokens/` — see [Device Tokens API](../apiDocs/core/device_token.md).

---

## Gotchas

- **Path vs contents:** `FIREBASE_CREDENTIALS` must be a **path**. Pasting JSON contents will
  fail (`credentials.Certificate` expects a file path here).
- **API version:** ensure **FCM API (V1)** is enabled; the legacy "Cloud Messaging API (Legacy)"
  server key is not used by `firebase-admin`.
- **Dead tokens self-heal:** when FCM reports a token unregistered, push.py auto-deactivates that
  `DeviceToken` — no action needed. Tokens that simply stop checking in are retired by a nightly
  sweep instead; see [`purge_device_tokens`](../commands/purge_device_tokens.md).
- **One project, many tenants:** a single Firebase project serves all tenants; messages are
  targeted by device token, not project.
- **A token from the wrong project fails loudly.** If a client is built against a different
  Firebase project than the service account, every send returns `SENDER_ID_MISMATCH` and those
  tokens are retired immediately. Check the client's `messagingSenderId` against the service
  account's `project_id` before assuming the tokens are bad.
- **iOS needs one more thing than this page lists.** Push to iOS requires an **APNs auth key**
  uploaded under Cloud Messaging → Apple app configuration. Without it, iOS sends fail with
  `ThirdPartyAuthError` — which is deliberately treated as *not the device's fault*, so those
  tokens are left active rather than being wrongly retired while the certificate is missing.
