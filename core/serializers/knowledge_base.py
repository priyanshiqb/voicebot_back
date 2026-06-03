from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from core.models.knowledge_base import KnowledgeBase, KBText, KBFiles, KBWebsite, KBQuestionAnswer, KBQuestions

from core.serializers.kb_text import KBTextSerializer, KBTextRefSerializer
from core.serializers.kb_files import KBFilesSerializer, KBFilesRefSerializer
from core.serializers.kb_website import KBWebsiteSerializer, KBWebsiteRefSerializer
from core.serializers.kb_question_answer import KBQuestionAnswerSerializer, KBQuestionAnswerRefSerializer
from core.serializers.kb_questions import KBQuestionsSerializer, KBQuestionsRefSerializer

class NestedKBTextSerializer(KBTextSerializer):
    id = serializers.UUIDField(required=False)
    flag = serializers.ChoiceField(choices=["create", "update", "delete"], required=False)

    class Meta(KBTextSerializer.Meta):
        fields = KBTextSerializer.Meta.fields + ("id", "flag")

    def to_internal_value(self, data):
        flag = data.get('flag')
        if flag in ['update', 'delete'] or ('id' in data and not flag):
            record_id = data.get('id')
            if record_id:
                try:
                    existing = KBText.objects.get(id=record_id, is_deleted=False)
                    for field in self.fields:
                        if field not in data and hasattr(existing, field):
                            if field not in ['tenant', 'file', 'flag']:
                                data[field] = getattr(existing, field)
                except KBText.DoesNotExist:
                    pass
        return super().to_internal_value(data)

class NestedKBFilesSerializer(KBFilesSerializer):
    id = serializers.UUIDField(required=False)
    flag = serializers.ChoiceField(choices=["create", "update", "delete"], required=False)
    file = serializers.FileField(required=False)

    class Meta(KBFilesSerializer.Meta):
        fields = KBFilesSerializer.Meta.fields + ("id", "flag")

    def to_internal_value(self, data):
        flag = data.get('flag')
        if flag in ['update', 'delete'] or ('id' in data and not flag):
            record_id = data.get('id')
            if record_id:
                try:
                    existing = KBFiles.objects.get(id=record_id, is_deleted=False)
                    for field in self.fields:
                        if field not in data and hasattr(existing, field):
                            if field not in ['tenant', 'file', 'flag']:
                                data[field] = getattr(existing, field)
                except KBFiles.DoesNotExist:
                    pass
        return super().to_internal_value(data)

    def validate(self, attrs):
        flag = attrs.get('flag')
        record_id = attrs.get('id')
        if flag == 'create' or (not record_id and not flag):
            if 'file' not in attrs:
                raise serializers.ValidationError({"file": "No file was submitted."})
        return attrs

class NestedKBWebsiteSerializer(KBWebsiteSerializer):
    id = serializers.UUIDField(required=False)
    flag = serializers.ChoiceField(choices=["create", "update", "delete"], required=False)

    class Meta(KBWebsiteSerializer.Meta):
        fields = KBWebsiteSerializer.Meta.fields + ("id", "flag")

    def to_internal_value(self, data):
        flag = data.get('flag')
        if flag in ['update', 'delete'] or ('id' in data and not flag):
            record_id = data.get('id')
            if record_id:
                try:
                    existing = KBWebsite.objects.get(id=record_id, is_deleted=False)
                    for field in self.fields:
                        if field not in data and hasattr(existing, field):
                            if field not in ['tenant', 'flag']:
                                data[field] = getattr(existing, field)
                except KBWebsite.DoesNotExist:
                    pass
        return super().to_internal_value(data)

    def validate(self, attrs):
        flag = attrs.get('flag')
        if flag == 'delete':
            return attrs

        url = attrs.get('url')
        request = self.context.get('request')
        tenant = None
        if request and request.user and not request.user.is_superuser:
            try:
                tenant = request.user.tenant_mapping.tenant
            except AttributeError:
                pass

        qs = KBWebsite.objects.filter(tenant=tenant, url=url, is_deleted=False)
        record_id = attrs.get('id')
        if record_id:
            qs = qs.exclude(pk=record_id)
        if qs.exists():
            raise serializers.ValidationError({"url": "This website URL has already been registered."})
        return attrs

class NestedKBQuestionsSerializer(KBQuestionsSerializer):
    id = serializers.UUIDField(required=False)
    flag = serializers.ChoiceField(choices=["create", "update", "delete"], required=False)

    class Meta(KBQuestionsSerializer.Meta):
        fields = KBQuestionsSerializer.Meta.fields + ("id", "flag")

    def to_internal_value(self, data):
        flag = data.get('flag')
        if flag in ['update', 'delete'] or ('id' in data and not flag):
            record_id = data.get('id')
            if record_id:
                try:
                    existing = KBQuestions.objects.get(id=record_id, is_deleted=False)
                    for field in self.fields:
                        if field not in data and hasattr(existing, field):
                            if field not in ['tenant', 'flag']:
                                data[field] = getattr(existing, field)
                except KBQuestions.DoesNotExist:
                    pass
        return super().to_internal_value(data)

class NestedKBQuestionAnswerSerializer(KBQuestionAnswerSerializer):
    id = serializers.UUIDField(required=False)
    flag = serializers.ChoiceField(choices=["create", "update", "delete"], required=False)
    questions = NestedKBQuestionsSerializer(many=True, required=False)

    class Meta(KBQuestionAnswerSerializer.Meta):
        fields = KBQuestionAnswerSerializer.Meta.fields + ("id", "flag")

    def to_internal_value(self, data):
        flag = data.get('flag')
        if flag in ['update', 'delete'] or ('id' in data and not flag):
            record_id = data.get('id')
            if record_id:
                try:
                    existing = KBQuestionAnswer.objects.get(id=record_id, is_deleted=False)
                    for field in self.fields:
                        if field not in data and hasattr(existing, field):
                            if field not in ['tenant', 'questions', 'flag']:
                                data[field] = getattr(existing, field)
                except KBQuestionAnswer.DoesNotExist:
                    pass
        return super().to_internal_value(data)

class KnowledgeBaseRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeBase
        fields = ("id", "name")

class KnowledgeBaseSerializer(TenantBaseModelSerializer):
    texts = NestedKBTextSerializer(many=True, required=False)
    files = NestedKBFilesSerializer(many=True, required=False)
    websites = NestedKBWebsiteSerializer(many=True, required=False)
    question_answers = NestedKBQuestionAnswerSerializer(many=True, required=False)

    class Meta(TenantBaseModelSerializer.Meta):
        model = KnowledgeBase
        fields = (
            "id", "tenant", "name", "texts", "files",
            "websites", "question_answers", "is_processed", "processing_status",
            "processing_error", "last_processed_at", "processing_history",
            "created_by", "updated_by", "deleted_by", "created_at", "updated_at", "deleted_at"
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "is_processed", "processing_status", "processing_error",
            "last_processed_at", "processing_history",
        )

    def validate(self, attrs):
        name = attrs.get('name')
        request = self.context.get('request')
        tenant = None
        if request and request.user and not request.user.is_superuser:
            try:
                tenant = request.user.tenant_mapping.tenant
            except AttributeError:
                pass
        
        qs = KnowledgeBase.objects.filter(tenant=tenant, name=name, is_deleted=False)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({"name": "A Knowledge Base with this name already exists."})
        return attrs

    def to_internal_value(self, data):
        # Normalize flat UUID strings/objects to dictionaries inside lists
        for field_name in ["texts", "files", "websites", "question_answers"]:
            if field_name in data and isinstance(data[field_name], list):
                normalized = []
                for item in data[field_name]:
                    if isinstance(item, (str, dict)) or hasattr(item, "id"):
                        if isinstance(item, (str, dict)):
                            if isinstance(item, str):
                                normalized.append({"id": item})
                            else:
                                normalized.append(item)
                        else:
                            normalized.append({"id": str(item.id)})
                data[field_name] = normalized
        return super().to_internal_value(data)

    def create(self, validated_data):
        texts_data = validated_data.pop('texts', [])
        files_data = validated_data.pop('files', [])
        websites_data = validated_data.pop('websites', [])
        question_answers_data = validated_data.pop('question_answers', [])

        request = self.context.get('request')
        tenant = validated_data.pop('tenant', None)
        if not tenant:
            if request and request.user and not request.user.is_superuser:
                try:
                    tenant = request.user.tenant_mapping.tenant
                except AttributeError:
                    pass

        kb = KnowledgeBase.objects.create(tenant=tenant, **validated_data)

        # Create sub KBText objects (link existing if ID passed, otherwise create new)
        for text_item in texts_data:
            text_item.pop('flag', None)
            text_id = text_item.pop('id', None)
            if text_id and KBText.objects.filter(id=text_id, tenant=tenant).exists():
                text_obj = KBText.objects.get(id=text_id, tenant=tenant)
            else:
                text_item['tenant'] = tenant
                text_obj = KBText.objects.create(**text_item)
            kb.texts.add(text_obj)

        # Create sub KBFiles objects
        for file_item in files_data:
            file_item.pop('flag', None)
            file_id = file_item.pop('id', None)
            if file_id and KBFiles.objects.filter(id=file_id, tenant=tenant).exists():
                file_obj = KBFiles.objects.get(id=file_id, tenant=tenant)
            else:
                file_item['tenant'] = tenant
                if 'original_name' not in file_item and 'file' in file_item:
                    file_item['original_name'] = file_item['file'].name
                file_obj = KBFiles.objects.create(**file_item)
            kb.files.add(file_obj)

        # Create sub KBWebsite objects
        for website_item in websites_data:
            website_item.pop('flag', None)
            website_id = website_item.pop('id', None)
            if website_id and KBWebsite.objects.filter(id=website_id, tenant=tenant).exists():
                web_obj = KBWebsite.objects.get(id=website_id, tenant=tenant)
            else:
                website_item['tenant'] = tenant
                web_obj = KBWebsite.objects.create(**website_item)
            kb.websites.add(web_obj)

        # Create sub KBQuestionAnswer objects
        for qa_item in question_answers_data:
            qa_item.pop('flag', None)
            qa_id = qa_item.pop('id', None)
            if qa_id and KBQuestionAnswer.objects.filter(id=qa_id, tenant=tenant).exists():
                qa_obj = KBQuestionAnswer.objects.get(id=qa_id, tenant=tenant)
            else:
                questions_data = qa_item.pop('questions', [])
                qa_item['tenant'] = tenant
                qa_obj = KBQuestionAnswer.objects.create(**qa_item)
                for q_item in questions_data:
                    q_item.pop('flag', None)
                    q_item.pop('id', None)
                    q_item['tenant'] = tenant
                    q_obj = KBQuestions.objects.create(**q_item)
                    qa_obj.questions.add(q_obj)
            kb.question_answers.add(qa_obj)

        return kb

    def update(self, instance, validated_data):
        texts_data = validated_data.pop('texts', None)
        files_data = validated_data.pop('files', None)
        websites_data = validated_data.pop('websites', None)
        question_answers_data = validated_data.pop('question_answers', None)

        request = self.context.get('request')
        tenant = validated_data.pop('tenant', None)
        if not tenant:
            if request and request.user and not request.user.is_superuser:
                try:
                    tenant = request.user.tenant_mapping.tenant
                except AttributeError:
                    pass
            else:
                tenant = instance.tenant

        # Update parent fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # Update texts using flag-based triggers
        if texts_data is not None:
            for item in texts_data:
                flag = item.pop('flag', None)
                item_id = item.pop('id', None)

                # Fallback: link existing if id but no flag (backward compatibility)
                if not flag and item_id:
                    if KBText.objects.filter(id=item_id, tenant=tenant).exists():
                        text_obj = KBText.objects.get(id=item_id, tenant=tenant)
                        instance.texts.add(text_obj)

                elif flag == 'create':
                    item['tenant'] = tenant
                    text_obj = KBText.objects.create(**item)
                    instance.texts.add(text_obj)

                elif flag == 'update' and item_id:
                    if KBText.objects.filter(id=item_id, tenant=tenant).exists():
                        text_obj = KBText.objects.get(id=item_id, tenant=tenant)
                        for attr, val in item.items():
                            setattr(text_obj, attr, val)
                        text_obj.save()

                elif flag == 'delete' and item_id:
                    if KBText.objects.filter(id=item_id, tenant=tenant).exists():
                        text_obj = KBText.objects.get(id=item_id, tenant=tenant)
                        instance.texts.remove(text_obj)
                        text_obj.soft_delete(user=request.user if request else None)

        # Update files
        if files_data is not None:
            for item in files_data:
                flag = item.pop('flag', None)
                item_id = item.pop('id', None)

                if not flag and item_id:
                    if KBFiles.objects.filter(id=item_id, tenant=tenant).exists():
                        file_obj = KBFiles.objects.get(id=item_id, tenant=tenant)
                        instance.files.add(file_obj)

                elif flag == 'create':
                    item['tenant'] = tenant
                    if 'original_name' not in item and 'file' in item:
                        item['original_name'] = item['file'].name
                    file_obj = KBFiles.objects.create(**item)
                    instance.files.add(file_obj)

                elif flag == 'update' and item_id:
                    if KBFiles.objects.filter(id=item_id, tenant=tenant).exists():
                        file_obj = KBFiles.objects.get(id=item_id, tenant=tenant)
                        for attr, val in item.items():
                            setattr(file_obj, attr, val)
                        file_obj.save()

                elif flag == 'delete' and item_id:
                    if KBFiles.objects.filter(id=item_id, tenant=tenant).exists():
                        file_obj = KBFiles.objects.get(id=item_id, tenant=tenant)
                        instance.files.remove(file_obj)
                        file_obj.soft_delete(user=request.user if request else None)

        # Update websites
        if websites_data is not None:
            for item in websites_data:
                flag = item.pop('flag', None)
                item_id = item.pop('id', None)

                if not flag and item_id:
                    if KBWebsite.objects.filter(id=item_id, tenant=tenant).exists():
                        web_obj = KBWebsite.objects.get(id=item_id, tenant=tenant)
                        instance.websites.add(web_obj)

                elif flag == 'create':
                    item['tenant'] = tenant
                    web_obj = KBWebsite.objects.create(**item)
                    instance.websites.add(web_obj)

                elif flag == 'update' and item_id:
                    if KBWebsite.objects.filter(id=item_id, tenant=tenant).exists():
                        web_obj = KBWebsite.objects.get(id=item_id, tenant=tenant)
                        for attr, val in item.items():
                            setattr(web_obj, attr, val)
                        web_obj.save()

                elif flag == 'delete' and item_id:
                    if KBWebsite.objects.filter(id=item_id, tenant=tenant).exists():
                        web_obj = KBWebsite.objects.get(id=item_id, tenant=tenant)
                        instance.websites.remove(web_obj)
                        web_obj.soft_delete(user=request.user if request else None)

        # Update Q&As
        if question_answers_data is not None:
            for item in question_answers_data:
                flag = item.pop('flag', None)
                item_id = item.pop('id', None)
                questions_data = item.pop('questions', None)

                if not flag and item_id:
                    if KBQuestionAnswer.objects.filter(id=item_id, tenant=tenant).exists():
                        qa_obj = KBQuestionAnswer.objects.get(id=item_id, tenant=tenant)
                        instance.question_answers.add(qa_obj)

                elif flag == 'create':
                    item['tenant'] = tenant
                    qa_obj = KBQuestionAnswer.objects.create(**item)
                    if questions_data:
                        for q_item in questions_data:
                            q_item.pop('flag', None)
                            q_item.pop('id', None)
                            q_item['tenant'] = tenant
                            q_obj = KBQuestions.objects.create(**q_item)
                            qa_obj.questions.add(q_obj)
                    instance.question_answers.add(qa_obj)

                elif flag == 'update' and item_id:
                    if KBQuestionAnswer.objects.filter(id=item_id, tenant=tenant).exists():
                        qa_obj = KBQuestionAnswer.objects.get(id=item_id, tenant=tenant)
                        for attr, val in item.items():
                            setattr(qa_obj, attr, val)
                        qa_obj.save()

                        # Questions recursive flags
                        if questions_data is not None:
                            for q_item in questions_data:
                                q_flag = q_item.pop('flag', None)
                                q_id = q_item.pop('id', None)

                                if not q_flag and q_id:
                                    if KBQuestions.objects.filter(id=q_id, tenant=tenant).exists():
                                        q_obj = KBQuestions.objects.get(id=q_id, tenant=tenant)
                                        qa_obj.questions.add(q_obj)

                                elif q_flag == 'create':
                                    q_item['tenant'] = tenant
                                    q_obj = KBQuestions.objects.create(**q_item)
                                    qa_obj.questions.add(q_obj)

                                elif q_flag == 'update' and q_id:
                                    if KBQuestions.objects.filter(id=q_id, tenant=tenant).exists():
                                        q_obj = KBQuestions.objects.get(id=q_id, tenant=tenant)
                                        for attr, val in q_item.items():
                                            setattr(q_obj, attr, val)
                                        q_obj.save()

                                elif q_flag == 'delete' and q_id:
                                    if KBQuestions.objects.filter(id=q_id, tenant=tenant).exists():
                                        q_obj = KBQuestions.objects.get(id=q_id, tenant=tenant)
                                        qa_obj.questions.remove(q_obj)
                                        q_obj.soft_delete(user=request.user if request else None)

                elif flag == 'delete' and item_id:
                    if KBQuestionAnswer.objects.filter(id=item_id, tenant=tenant).exists():
                        qa_obj = KBQuestionAnswer.objects.get(id=item_id, tenant=tenant)
                        instance.question_answers.remove(qa_obj)
                        qa_obj.soft_delete(user=request.user if request else None)

        return instance

class KnowledgeBaseListSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeBase
        fields = ("id", "name", "is_processed", "processing_status", "created_at")

class KnowledgeBaseDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="name")
    value = serializers.UUIDField(source="id")
