* [Welcome](README.md)

* **🔌 WebSockets**
  * [WS Event Protocol](wsDocs/instruction.md)

* **🤖 Voicebot Agent Tokens**
  * [Tenant Scoping & Access Matrix](apiDocs/core/vb_agent_tenant_scoping.md)

* **🌐 Core APIs**
  * [Agent Management](apiDocs/core/agent.md)
  * [Agent Call Logs](apiDocs/core/agent_call_log.md)
  * [Agent Templates](apiDocs/core/agent_template.md)
  * [Knowledge Base](apiDocs/core/knowledge_base.md)
  * [Agent Call Recording (MP3)](apiDocs/core/agent_call_recording.md)
  * [Probing Questions](apiDocs/core/probing_question.md)
  * [Custom Voice Records](apiDocs/core/custom_voice.md)
  * [Contacts](apiDocs/core/contact.md)
  * [Contact Import - Bulk CSV](apiDocs/core/contact_import.md)
  * [Company Directory](apiDocs/core/directory_contact.md)

* **🛒 Product & Service Catalog**
  * [Catalog Items](apiDocs/core/catalog_item.md)
  * [Catalog Variants - Pricing](apiDocs/core/catalog_variant.md)
  * [Catalog Categories](apiDocs/core/catalog_category.md)
  * [Catalog Item Media](apiDocs/core/catalog_item_media.md)

* **📣 Campaigns**
  * [Overview & Complete Flow](campaigns/README.md)
  * [Campaigns API](apiDocs/core/campaign.md)
  * [Campaign Runner - Machine API](apiDocs/core/campaign_runner.md)

* **📅 Scheduling & Booking APIs**
  * [Business Hours](apiDocs/core/business_hours.md)
  * [Event Templates](apiDocs/core/event_template.md)
  * [Host Event Skills](apiDocs/core/event_skill.md)
  * [Availability Profiles](apiDocs/core/availability_profile.md)
  * [Availability Exceptions - Leave](apiDocs/core/availability_exception.md)
  * [Available Slots](apiDocs/core/availability_slots.md)
  * [Bookings](apiDocs/core/booking.md)
  * [Calendar Integrations](apiDocs/core/calendar_integration.md)
  * [Device Tokens - Push](apiDocs/core/device_token.md)
  * [Public Reschedule](apiDocs/core/public_reschedule.md)

* **🔑 External App Credentials**
  * [Overview & .env Map](external-app-creds/README.md)
  * [Firebase Cloud Messaging](external-app-creds/01-firebase-fcm.md)
  * [Google Calendar](external-app-creds/02-google-calendar.md)

* **🔀 Scheduling Flows**
  * [Overview & Endpoint Map](flows/README.md)
  * [Complete API Walkthrough (V4)](flows/booking-v4.md)
  * [Implementation Reference (internals)](flows/booking-flow-internals.md)
  * [1. Tenant Setup](flows/01-setup.md)
  * [2. Booking Lifecycle](flows/02-booking-lifecycle.md)
  * [3. Leave & Reschedule](flows/03-leave-and-reschedule.md)
  * [4. Google Calendar Sync](flows/04-google-calendar-sync.md)
  * [5. Notifications & Push](flows/05-notifications-and-push.md)
  * [6. AI Agent Booking](flows/06-ai-agent-booking.md)

* **📝 Onboard APIs**
  * [Plans](apiDocs/onboard/plans.md)
  * [Signup Flow](apiDocs/onboard/signup_flow.md)
  * [Check Phone Availability](apiDocs/onboard/check_phone.md)
  <!-- * [Postal Codes](apiDocs/onboard/postal_codes.md) -->
  * [Geography - Country](apiDocs/onboard/country.md)
  * [Geography - Admin1](apiDocs/onboard/admin1.md)
  * [Geography - Admin2](apiDocs/onboard/admin2.md)
  * [Geography - Admin3](apiDocs/onboard/admin3.md)
  * [Geography - Place](apiDocs/onboard/place.md)
  * [Geography - Postal Codes Dropdown](apiDocs/onboard/postal_code_dropdown.md)
  * [Languages](apiDocs/onboard/languages.md)
  * [Timezones](apiDocs/onboard/timezones.md)
  * [Webhooks](apiDocs/onboard/webhooks.md)
  * [Password Reset](apiDocs/onboard/password_reset.md)
  * [Forgot Password Resend](apiDocs/onboard/forgot_password_resend.md)

* **👤 User APIs**
  * [Authentication](apiDocs/user/auth.md)
  * [Sessions](apiDocs/user/sessions.md)
  * [Users](apiDocs/user/users.md)
  * [Forgot Password Resend](apiDocs/user/forgot_password_resend.md)

* **🏢 Tenant APIs**
  * [Tenant Settings](apiDocs/tenant/settings.md)
  * [Email Configuration - SMTP](apiDocs/tenant/email_configuration.md)

* **🧹 Maintenance Commands**
  * [Overview - Which One Do I Want?](commands/README.md)
  * [Abandoned Upload Sweep](commands/cleanup_pending_uploads.md)
  * [Orphaned Object Audit](commands/audit_orphaned_objects.md)
  * [Knowledge Base Files Cleanup](commands/cleanup_orphaned_kb_files.md)

<!-- * **📐 Architecture**
  * [S3 Storage Service](architecture/s3-storage-service.md) -->
