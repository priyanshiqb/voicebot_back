from core.admin.kb_text_admin import KBTextAdmin
from core.admin.kb_files_admin import KBFilesAdmin
from core.admin.kb_questions_admin import KBQuestionsAdmin
from core.admin.kb_question_answer_admin import KBQuestionAnswerAdmin
from core.admin.kb_website_admin import KBWebsiteAdmin
from core.admin.knowledge_base_admin import KnowledgeBaseAdmin
from core.admin.kb_etl_tracker_admin import KBETLTrackerAdmin
from core.admin.agent_template_admin import AgentTemplateAdmin
from core.admin.probing_question_admin import ProbingQuestionAdmin
from core.admin.agent_admin import AgentAdmin
from core.admin.agent_call_log_admin import AgentCallLogAdmin

# Scheduling & Booking (Phase 1)
from core.admin.event_template_admin import EventTemplateAdmin
from core.admin.user_event_skill_admin import UserEventSkillAdmin
from core.admin.availability_profile_admin import (
    AvailabilityProfileAdmin,
    AvailabilityWindowAdmin,
)
from core.admin.availability_exception_admin import AvailabilityExceptionAdmin
from core.admin.booking_admin import BookingAdmin
from core.admin.notification_log_admin import NotificationLogAdmin
from core.admin.calendar_integration_admin import CalendarIntegrationAdmin
from core.admin.device_token_admin import DeviceTokenAdmin
from core.admin.custom_voice_admin import CustomVoiceAdmin
from core.admin.agent_call_recording_admin import AgentCallRecordingAdmin

# Contacts
from core.admin.business_hours_admin import (
    BusinessHoursAdmin,
    BusinessHoursIntervalAdmin,
)
from core.admin.contact_admin import ContactAdmin
from core.admin.directory_contact_admin import DirectoryContactAdmin
from core.admin.contact_import_admin import (
    ContactImportFileAdmin,
    ContactImportRowAdmin,
    ContactImportSessionAdmin,
)

# Campaigns
from core.admin.campaign_admin import CampaignAdmin
from core.admin.campaign_member_admin import CampaignMemberAdmin

__all__ = [
    "KBTextAdmin",
    "KBFilesAdmin",
    "KBQuestionsAdmin",
    "KBQuestionAnswerAdmin",
    "KBWebsiteAdmin",
    "KnowledgeBaseAdmin",
    "KBETLTrackerAdmin",
    "AgentTemplateAdmin",
    "ProbingQuestionAdmin",
    "AgentAdmin",
    "AgentCallLogAdmin",
    "EventTemplateAdmin",
    "UserEventSkillAdmin",
    "AvailabilityProfileAdmin",
    "AvailabilityWindowAdmin",
    "AvailabilityExceptionAdmin",
    "BookingAdmin",
    "NotificationLogAdmin",
    "CalendarIntegrationAdmin",
    "DeviceTokenAdmin",
    "CustomVoiceAdmin",
    "AgentCallRecordingAdmin",
    "BusinessHoursAdmin",
    "BusinessHoursIntervalAdmin",
    "ContactAdmin",
    "DirectoryContactAdmin",
    "ContactImportFileAdmin",
    "ContactImportSessionAdmin",
    "ContactImportRowAdmin",
    "CampaignAdmin",
    "CampaignMemberAdmin",
]


