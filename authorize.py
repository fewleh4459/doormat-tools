"""
One-time OAuth helper. Run this ONCE on your local PC (not on Replit)
to generate an OAuth refresh-token JSON for the doormat Drive watcher.

Why you need this:
  Google service accounts have no storage quota — they can't create files
  in user-owned Drive folders. This script generates OAuth credentials
  that let the watcher act AS a user (e.g. oliver@beaudax.co.uk),
  using that user's Drive quota.

Setup (one-time):
  1. Go to https://console.cloud.google.com/apis/credentials
  2. Click "Create credentials" → "OAuth client ID"
  3. Application type: Desktop app
  4. Name: "Doormat Watcher"
  5. Click Create → click DOWNLOAD JSON on the new client
  6. Save the downloaded file as `oauth_client.json` next to this script
  7. Run:  python authorize.py
  8. Browser opens → log in as the user whose Drive quota you want to use
     (e.g. oliver@beaudax.co.uk) → approve access
  9. Script writes `oauth_token.json` to this folder
 10. Copy the ENTIRE CONTENTS of oauth_token.json
 11. In Replit, open Secrets → edit GOOGLE_CREDENTIALS_JSON → paste the
     oauth_token.json content (overwriting the old service account JSON)
 12. Restart drive_watcher.py — uploads will now work

This script only needs to be run ONCE. The refresh token doesn't expire
as long as the OAuth client stays in "Production" mode (or is kept in
"Testing" mode with the user added as a test user).

Note: your `oauth_client.json` and `oauth_token.json` are secrets — do
not commit them. They're already covered by the repo's .gitignore.
"""

import json
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]
CLIENT_SECRETS_FILE = "oauth_client.json"
OUTPUT_FILE = "oauth_token.json"


def main():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        print(f"ERROR: {CLIENT_SECRETS_FILE} not found in current directory.")
        print("See the comment at the top of this script for setup steps.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
    print("Opening browser for Google authentication…")
    print("Log in as the user whose Drive quota you want the watcher to use")
    print("(typically oliver@beaudax.co.uk).")
    creds = flow.run_local_server(
        port=0,
        prompt="consent",            # force refresh token
        access_type="offline",
    )

    if not creds.refresh_token:
        print("\nERROR: no refresh_token received. Try again — you may need to revoke")
        print("the OAuth client at https://myaccount.google.com/permissions first.")
        sys.exit(1)

    output = {
        "type": "oauth_user",
        "refresh_token": creds.refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "token_uri": creds.token_uri,
        "scopes": list(creds.scopes) if creds.scopes else SCOPES,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print()
    print("✓ Authentication successful.")
    print(f"✓ Refresh token saved to: {os.path.abspath(OUTPUT_FILE)}")
    print()
    print("Next steps:")
    print("  1. Open oauth_token.json, select all, copy.")
    print("  2. In Replit → Secrets → edit GOOGLE_CREDENTIALS_JSON → paste → save.")
    print("  3. Restart drive_watcher.py on Replit.")
    print()
    print("Uploads will now work because the watcher authenticates as your user.")


if __name__ == "__main__":
    main()
