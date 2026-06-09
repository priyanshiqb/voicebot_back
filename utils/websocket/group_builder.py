class GroupNameBuilder:
    """
    Utility to dynamically build Django Channels group names 
    based on a GroupRegistry configuration.
    """

    @staticmethod
    def build(group_config: dict, **kwargs):
        prefix = group_config.get("prefix")
        if not prefix:
            raise ValueError("Group config must define a 'prefix'")

        required_keys = group_config.get("context", [])
        
        # Support both spellings gracefully
        separator = group_config.get("separator", group_config.get("seprator", "_"))

        parts = [str(prefix)]

        for key in required_keys:
            value = kwargs.get(key)

            if value is None:
                raise ValueError(f"Missing required context: {key}")

            parts.append(str(value))
            
        return separator.join(parts)
