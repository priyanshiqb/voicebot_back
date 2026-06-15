from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from core.common.models.kb_base_model import KBBaseModel

def kb_file_upload_path(instance, filename):
    return f"t-{instance.tenant_id}/kb_files/{filename}"

def validate_file_size(value):
    max_size = getattr(settings, "DATA_UPLOAD_MAX_MEMORY_SIZE", 10 * 1024 * 1024)
    if value.size > max_size:
        raise ValidationError(f"File size should be less than {max_size / (1024 * 1024)} MB.")
    return value

class KBText(KBBaseModel):
    title = models.CharField(max_length=255, help_text="Title of the text block")
    description = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "core_kb_texts"
        verbose_name = "KB Text"
        verbose_name_plural = "KB Texts"

    def __str__(self):
        return self.title


class KBFiles(KBBaseModel):
    file = models.FileField(
        upload_to=kb_file_upload_path,
        validators=[validate_file_size]
    )
    original_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Clean original name of the uploaded file"
    )
    file_size_in_bytes = models.BigIntegerField(default=0)
    extension = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        help_text="File extension (e.g., '.pdf', '.docx', '.txt')"
    )

    class Meta:
        db_table = "core_kb_files"
        verbose_name = "KB File"
        verbose_name_plural = "KB Files"

    def save(self, *args, **kwargs):
        if self.pk:
            old_instance = type(self).objects.filter(pk=self.pk).first()
            if old_instance and old_instance.file and old_instance.file != self.file:
                try:
                    old_instance.file.delete(save=False)
                except Exception:
                    pass
        if self.file:
            if not self.original_name:
                self.original_name = self.file.name
            try:
                self.file_size_in_bytes = self.file.size
            except Exception:
                pass
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.file:
            try:
                self.file.delete(save=False)
            except Exception:
                pass
        super().delete(*args, **kwargs)

    @classmethod
    def get_tenant_file_stats(cls, tenant_id):
        """
        Returns a summary of file stats for a tenant directory.
        """
        from django.db.models import Sum
        qs = cls.all_objects.filter(tenant_id=tenant_id)
        data = {
            "total_files": qs.count(),
            "total_size": qs.aggregate(size=Sum("file_size_in_bytes"))["size"] or 0,
            "active_files": qs.filter(is_deleted=False).count(),
            "active_size": qs.filter(is_deleted=False).aggregate(size=Sum("file_size_in_bytes"))["size"] or 0,
            "deleted_files": qs.filter(is_deleted=True).count(),
            "deleted_size": qs.filter(is_deleted=True).aggregate(size=Sum("file_size_in_bytes"))["size"] or 0,
        }
        return data

    def __str__(self):
        return self.original_name or self.file.name


class KBQuestions(KBBaseModel):
    question = models.TextField(help_text="Standard question text")

    class Meta:
        db_table = "core_kb_questions"
        verbose_name = "KB Question"
        verbose_name_plural = "KB Questions"

    def __str__(self):
        return self.question[:50]


class KBQuestionAnswer(KBBaseModel):
    title = models.CharField(max_length=255, help_text="Title of the Q&A pair")
    questions = models.ManyToManyField(KBQuestions, related_name="question_answers")
    answer = models.TextField()

    class Meta:
        db_table = "core_kb_question_answers"
        verbose_name = "KB Question Answer"
        verbose_name_plural = "KB Question Answers"

    def __str__(self):
        return self.title


class KBWebsite(KBBaseModel):
    url = models.URLField(max_length=500)
    content = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "core_kb_websites"
        verbose_name = "KB Website"
        verbose_name_plural = "KB Websites"
        constraints = [
            models.UniqueConstraint(
                fields=["url", "tenant"],
                name="unique_kb_website_url_tenant",
            )
        ]

    def soft_delete(self, user=None):
        from django.utils import timezone
        self.is_deleted = True
        self.deleted_at = timezone.now()
        if user:
            self.deleted_by = user
        
        original_url = self.url
        deleted_suffix = f"-deleted-{self.deleted_at.strftime('%Y%m%d%H%M%S')}"
        if '#' in original_url:
            self.url = original_url.replace('#', f'{deleted_suffix}#')
        else:
            self.url = f"{original_url}{deleted_suffix}"
        
        update_fields = ["is_deleted", "deleted_at", "url"]
        if user:
            update_fields.append("deleted_by")
        self.save(update_fields=update_fields)

    def __str__(self):
        return self.url


class KnowledgeBase(KBBaseModel):
    name = models.CharField(max_length=255, help_text="Name of the knowledge base")

    texts = models.ManyToManyField(
        KBText,
        blank=True,
        related_name="knowledge_bases",
    )
    files = models.ManyToManyField(
        KBFiles,
        blank=True,
        related_name="knowledge_bases",
    )
    websites = models.ManyToManyField(
        KBWebsite,
        blank=True,
        related_name="knowledge_bases",
    )
    question_answers = models.ManyToManyField(
        KBQuestionAnswer,
        blank=True,
        related_name="knowledge_bases",
    )

    class Meta:
        db_table = "core_knowledge_bases"
        verbose_name = "Knowledge Base"
        verbose_name_plural = "Knowledge Bases"
        constraints = [
            models.UniqueConstraint(
                fields=["name", "tenant"],
                name="unique_knowledge_base_name_tenant",
            )
        ]

    def soft_delete(self, user=None):
        from django.db import transaction
        from django.utils import timezone
        with transaction.atomic():
            # Lock row
            kb = KnowledgeBase.objects.select_for_update().get(pk=self.pk)
            kb.is_deleted = True
            kb.deleted_at = timezone.now()
            if user:
                kb.deleted_by = user
            
            kb.name = f"{kb.name} -deleted-{kb.deleted_at.strftime('%Y%m%d%H%M%S')}"
            
            update_fields = ["is_deleted", "deleted_at", "name"]
            if user:
                update_fields.append("deleted_by")
            kb.save(update_fields=update_fields)

            # Cascade soft delete to all M2M relations
            for text in kb.texts.all():
                text.soft_delete(user=user)
            for file in kb.files.all():
                file.soft_delete(user=user)
            for website in kb.websites.all():
                website.soft_delete(user=user)
            for qa in kb.question_answers.all():
                for question in qa.questions.all():
                    question.soft_delete(user=user)
                qa.soft_delete(user=user)

    def __str__(self):
        return self.name
