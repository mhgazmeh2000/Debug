"""
اسکریپت کمکی دریافت Refresh Token برای Gmail OAuth 2.0 SMTP

مراحل:
1. Gmail API را در Google Cloud Console فعال کنید
2. OAuth Consent Screen بسازید (Test user = ایمیل فرستنده)
3. OAuth Client ID (Web application) بسازید
4. این اسکریپت را اجرا کنید
5. URL نمایش داده شده را در مرورگر باز کنید
6. Authorization Code را کپی و Paste کنید
7. Refresh Token نمایش داده می‌شود

Usage:
    python tools/get_oauth_token.py
"""

import sys
import urllib.parse
import requests


def main():
    print("=" * 60)
    print("Gmail OAuth 2.0 — Refresh Token Generator")
    print("=" * 60)
    print()

    client_id = input("Google Client ID: ").strip()
    client_secret = input("Google Client Secret: ").strip()
    gmail_address = input("Gmail address (sender): ").strip()

    if not all([client_id, client_secret, gmail_address]):
        print("Error: All fields are required!")
        sys.exit(1)

    # Step 1: Build authorization URL
    redirect_uri = "http://localhost"
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "https://mail.google.com/",
            "access_type": "offline",
            "prompt": "consent",
        })
    )

    print()
    print("Step 1: Open this URL in your browser:")
    print()
    print(f"  {auth_url}")
    print()
    print("Step 2: Grant access, then copy the Authorization Code from the URL.")
    print()

    auth_code = input("Authorization Code: ").strip()
    if not auth_code:
        print("Error: Authorization Code is required!")
        sys.exit(1)

    # Step 3: Exchange code for refresh token
    print()
    print("Exchanging code for tokens...")
    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": auth_code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    refresh_token = data.get("refresh_token")
    access_token = data.get("access_token")
    expires_in = data.get("expires_in", 0)

    if not refresh_token:
        print()
        print("Error: No refresh_token in response!")
        print(f"Response: {data}")
        print()
        print("Possible causes:")
        print("  - Gmail API is not enabled in your Google Cloud project")
        print("  - Authorization Code has already been used (generate a new one)")
        print("  - access_type=offline was not specified")
        sys.exit(1)

    print()
    print("=" * 60)
    print("SUCCESS! Add these to your .env file:")
    print("=" * 60)
    print()
    print(f"MAIL_SERVER=smtp.gmail.com")
    print(f"MAIL_PORT=587")
    print(f"MAIL_USE_TLS=1")
    print(f"MAIL_USE_OAUTH=1")
    print(f"MAIL_USERNAME={gmail_address}")
    print(f"MAIL_OAUTH_CLIENT_ID={client_id}")
    print(f"MAIL_OAUTH_CLIENT_SECRET={client_secret}")
    print(f"MAIL_OAUTH_REFRESH_TOKEN={refresh_token}")
    print()
    print(f"Access Token (expires in {expires_in}s): {access_token[:20]}...")
    print()
    print("Note: Keep the Refresh Token secret! It never expires unless revoked.")


if __name__ == "__main__":
    main()
