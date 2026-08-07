# 🚀 Voicebot Developer API & WebSocket Portal

Welcome to the Voicebot backend developer documentation. This portal serves as a central hub for REST API design patterns and WebSocket event lifecycles.

Use the sidebar navigation to explore the developer resources.

---

## 🔌 WebSocket Documentation

* **[WebSocket Instructions](/#/wsDocs/instruction.md)**: Real-time audio and status streaming flows, event structures, and connection states.

---

## 🌐 API Reference

* **[Authentication APIs](/#/apiDocs/user/auth.md)**: JWT session lifetimes and refresh tokens.
* **[Signup & Onboarding Flows](/#/apiDocs/onboard/signup_flow.md)**: Onboarding steps, parameters, and verification requirements.
* **[Agent Management](/#/apiDocs/core/agent.md)**: CRUD operations for voice agents and templates.
* **[Email Configuration](/#/apiDocs/tenant/email_configuration.md)**: a tenant's own SMTP credentials.

> ⚠️ **Booking email does not send until a tenant configures SMTP.** There is no fallback to a
> platform sender — an unconfigured tenant's customers receive no confirmations, reminders or
> cancellations. Setup is two steps: create and verify a configuration, then **assign** it in
> [Tenant Settings](/#/apiDocs/tenant/settings.md).

---

## 🤖 Integrating a Voicebot Agent Token

Building the voice runner, or anything that authenticates with a `vb_…` agent token? Start here:

* **[Tenant Scoping & Access Matrix](/#/apiDocs/core/vb_agent_tenant_scoping.md)**: the `X-Tenant-ID`
  header (**required on every request**), exactly which endpoints an agent token may call, and
  where the `tenant` on records it creates comes from.

> ⚠️ **Breaking changes** for existing runners are listed on that page: `X-Tenant-ID` is now
> mandatory everywhere, and `GET /core/agents/` and `/core/agents/dropdown/` now return 403.
