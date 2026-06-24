import os
import django
from django.conf import settings
from django.urls import get_resolver

# Ensure Django is set up
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
django.setup()

def get_views():
    resolver = get_resolver()
    views_info = []

    def extract_views(urlpatterns, prefix=""):
        for pattern in urlpatterns:
            if hasattr(pattern, 'url_patterns'):
                # Include/RegexURLResolver
                extract_views(pattern.url_patterns, prefix + str(pattern.pattern))
            else:
                # URLPattern
                callback = pattern.callback
                if hasattr(callback, 'view_class'):
                    view_class = callback.view_class
                    view_name = view_class.__name__
                    module = view_class.__module__
                    
                    # Extract methods from action map if it's a ViewSet
                    methods = []
                    if hasattr(callback, 'actions') and callback.actions:
                        methods = list(callback.actions.keys())
                    elif hasattr(view_class, 'http_method_names'):
                        methods = [m.lower() for m in view_class.http_method_names if hasattr(view_class, m)]
                    
                    permissions = []
                    if hasattr(view_class, 'permission_classes'):
                        permissions = [p.__name__ for p in view_class.permission_classes]
                    
                    views_info.append({
                        'module': module,
                        'view': view_name,
                        'path': prefix + str(pattern.pattern),
                        'methods': [m.upper() for m in methods] if methods else ["ALL"],
                        'permissions': permissions
                    })
                elif hasattr(callback, '__name__'):
                    # Sometimes ViewSet.as_view() returns a function, but stores the class in cls
                    if hasattr(callback, 'cls'):
                        view_class = callback.cls
                        view_name = view_class.__name__
                        module = view_class.__module__
                        
                        methods = []
                        if hasattr(callback, 'actions') and callback.actions:
                            methods = list(callback.actions.keys())
                        elif hasattr(view_class, 'http_method_names'):
                            methods = [m.lower() for m in view_class.http_method_names if hasattr(view_class, m)]
                        
                        permissions = []
                        if hasattr(view_class, 'permission_classes'):
                            permissions = [p.__name__ for p in view_class.permission_classes]
                        
                        views_info.append({
                            'module': module,
                            'view': view_name,
                            'path': prefix + str(pattern.pattern),
                            'methods': [m.upper() for m in methods] if methods else ["ALL"],
                            'permissions': permissions
                        })
                    else:
                        views_info.append({
                            'module': callback.__module__,
                            'view': callback.__name__,
                            'path': prefix + str(pattern.pattern),
                            'methods': ["Function based view"],
                            'permissions': ["Unknown"]
                        })

    extract_views(resolver.url_patterns)
    
    # Format as markdown table
    print("| App/Module | View Class | Endpoint | Methods | Allowed User Types / Permissions |")
    print("|---|---|---|---|---|")
    
    # deduplicate view_class
    seen = {}
    for info in views_info:
        key = f"{info['module']}.{info['view']}"
        if key not in seen:
            seen[key] = info
        else:
            # merge methods
            for m in info['methods']:
                if m not in seen[key]['methods']:
                    seen[key]['methods'].append(m)

    for info in sorted(seen.values(), key=lambda x: x['module']):
        methods_str = ", ".join(info['methods'])
        perms_str = ", ".join(info['permissions']) if info['permissions'] else "AllowAny"
        print(f"| `{info['module']}` | `{info['view']}` | `{info['path']}` | {methods_str} | {perms_str} |")

if __name__ == '__main__':
    get_views()
