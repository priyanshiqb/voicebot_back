from rest_framework import serializers


class HybridRelatedField(serializers.PrimaryKeyRelatedField):

    def __init__(self, **kwargs):

        self.serializer_class = kwargs.pop("serializer", None)
        self.filter_deleted = kwargs.pop("filter_deleted", True)

        if not kwargs.get("required", True):
            kwargs["allow_null"] = True

        super().__init__(**kwargs)

        if (
            self.serializer_class is not None
            and not issubclass(
                self.serializer_class,
                serializers.Serializer
            )
        ):
            raise TypeError(
                '"serializer" must be a serializer class'
            )

    def use_pk_only_optimization(self):
        return False

    def to_representation(self, value):

        if value is None:
            return None

        if (
            self.filter_deleted
            and hasattr(value, "is_deleted")
            and value.is_deleted
        ):
            return None

        if self.serializer_class:
            return self.serializer_class(
                value,
                context=self.context
            ).data

        return super().to_representation(value)