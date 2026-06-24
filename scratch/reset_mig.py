from django.db import connection

def reset_agent_token_migration():
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS user_agent_tokens CASCADE;")
        cursor.execute("DELETE FROM django_migrations WHERE app='user' AND name='0011_alter_customuser_user_type_agenttoken';")
    print("Migration reset successfully.")
