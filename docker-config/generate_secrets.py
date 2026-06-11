#!/usr/bin/env python3
import secrets
import subprocess
import sys

def main():
    print("==========================================================")
    # 1. Django Secret Key
    django_secret = secrets.token_urlsafe(50)
    
    # 2. Postgres Database Password (using hex to guarantee strictly alphanumeric safety for URI parsers)
    postgres_pass = secrets.token_hex(24)
    
    # 3. pgAdmin Web UI Password
    pgadmin_pass = secrets.token_urlsafe(16)
    
    # 4. Grafana Admin Password
    grafana_pass = secrets.token_urlsafe(16)
    
    # 5. Gateway Basic Auth Plaintext
    basic_auth_plain = secrets.token_urlsafe(16)
    
    print("Generating Secure Credentials...")
    print("==========================================================")
    print(f"DJANGO_SECRET_KEY={django_secret}")
    print(f"POSTGRES_PASSWORD={postgres_pass}")
    print(f"PGADMIN_PASSWORD={pgadmin_pass}")
    print(f"GF_SECURITY_ADMIN_PASSWORD={grafana_pass}")
    print(f"GATEWAY_BASIC_AUTH_PLAINTEXT={basic_auth_plain}")
    
    # 6. Gateway Basic Auth Bcrypt Hash (using Docker with Caddy)
    print("\nContacting Docker to generate Caddy Bcrypt Hash...")
    try:
        cmd = [
            "docker", "run", "--rm", 
            "caddy:2.7-alpine", 
            "caddy", "hash-password", 
            "--plaintext", basic_auth_plain
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        raw_hash = result.stdout.strip()
        escaped_hash = raw_hash.replace("$", "$$")
        print(f"GATEWAY_BASIC_AUTH_PASSWORD_HASH={escaped_hash}")
        print("==========================================================")
        print("Success! Copy and paste the variables above into your production env files.")
        print("Note: The Basic Auth Hash has already been double-escaped ($$) for Compose compatibility.")
    except Exception as e:
        print("Could not run Docker to generate Caddy Bcrypt Hash automatically.")
        print(f"Error details: {e}")
        print("Please generate the hash manually by running the following command in your terminal:")
        print(f"  docker run --rm caddy:2.7-alpine caddy hash-password --plaintext '{basic_auth_plain}'")
        print("Remember to replace all single '$' with double '$$' when pasting into the .env file!")
        print("==========================================================")

if __name__ == "__main__":
    main()
