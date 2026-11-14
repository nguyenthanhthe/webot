# End-to-End Account Provisioning Pipeline: Temp Email + Wolvesville Farm

This document details the technical architecture and workflow for farm account provisioning using zero-config [Mail.tm](https://api.mail.tm) (`MailTmClient`) or self-hosted [`dreamhunter2333/cloudflare_temp_email`](https://github.com/dreamhunter2333/cloudflare_temp_email) (`TempEmailClient`).

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ACCOUNT PROVISIONING PIPELINE                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
           1. Generate Mailbox         ▼
   ┌────────────────────────────────────────────────────────┐
   │ Temp Email Provider (wvbot/temp_email.py)              │
   │  • MailTmClient (api.mail.tm, zero setup)              │
   │  • TempEmailClient (/admin/new_address + x-admin-auth) │
   │  Returns: Mailbox(address, jwt)                        │
   └───────────────────────────────────┬────────────────────┘
                                       │
           2. Explicit Browser Step    ▼
   ┌────────────────────────────────────────────────────────┐
   │ Operator Browser / Explicit register_fn Callback       │
   │  • Interactive Turnstile/human verification if prompted│
   │  • Submits sign-up { email, username, password }       │
   │  • Note: wvbot includes no CAPTCHA/Turnstile bypasses  │
   └───────────────────────────────────┬────────────────────┘
                                       │
           3. Poll Inbox & Verify      ▼
   ┌────────────────────────────────────────────────────────┐
   │ Verification Listener (wvbot.temp_email)               │
   │  • Polls Mail.tm /messages or /api/parsed_mails        │
   │  • Extracts 6-digit code or confirmation URL           │
   └───────────────────────────────────┬────────────────────┘
                                       │
           4. Mint Refresh Token       ▼
   ┌────────────────────────────────────────────────────────┐
   │ Token Acquisition                                      │
   │  • signInWithEmailAndPassword in authenticated browser │
   │  • Retrieves durable Firebase refreshToken             │
   └───────────────────────────────────┬────────────────────┘
                                       │
           5. Dynamic Cluster Join     ▼
   ┌────────────────────────────────────────────────────────┐
   │ Wolvesville Farm Cluster (:8787)                       │
   │  • POST /api/accounts { name, refresh_token }          │
   │  • Hot-launches new isolated bot worker thread         │
   │  • Begins autonomous 24/7 XP farming immediately       │
   └────────────────────────────────────────────────────────┘
```

---

## 2. Temp Email Providers

### A. Zero-Config Mail.tm (`MailTmClient`, default `--url auto`)
Requires no worker deployment or admin secret: queries active domains on `https://api.mail.tm`, creates an account via `POST /accounts`, mints a bearer token via `POST /token`, and polls `GET /messages`.

### B. Self-Hosted Cloudflare Temp Email (`TempEmailClient`)
Runs on Cloudflare Workers (`dreamhunter2333/cloudflare_temp_email`):

- **Create address:** `POST https://<worker-url>/admin/new_address` with `x-admin-auth: <ADMIN_PASSWORD>` and optional `x-custom-auth: <SITE_PASSWORD>`:
  ```json
  {
    "enablePrefix": true,
    "name": "wvbot_alpha",
    "domain": "yourdomain.com"
  }
  ```
  Returns `{"jwt": "...", "address": "wvbot_alpha@yourdomain.com"}`.
- **Poll parsed mail:** `GET https://<worker-url>/api/parsed_mails?limit=20` with `Authorization: Bearer <JWT>`.

---

## 3. Python API & CLI (`wvbot/temp_email.py`, `tools/temp_email.py`)

Implementation lives in `wvbot/temp_email.py`, with `tools/temp_email.py` providing a thin CLI wrapper and re-exports:

```python
from wvbot.temp_email import MailTmClient, TempEmailClient

# Option 1: Zero-config Mail.tm
client = MailTmClient()
mailbox = client.create_mailbox(name="bot_acc")

# Option 2: Self-hosted Cloudflare Temp Email
cf_client = TempEmailClient(
    base_url="https://mail.example.com",
    admin_password="your_admin_password",
)
mailbox = cf_client.create_mailbox(name="bot_acc", domain="yourdomain.com")
print(f"Created: {mailbox.address}")

code = cf_client.wait_for_verification_code(mailbox.jwt, timeout_sec=120)
print(f"Received verification code: {code}")
```

`AccountProvisioner` (`wvbot/provision.py`) requires an explicit `register_fn` callback (for example, your own visible browser Playwright/Marionette flow where an operator handles Turnstile if challenged) and fails fast when none is configured.

CLI usage:

```bash
python tools/temp_email.py create --url auto --name bot_acc
python tools/temp_email.py wait --url auto --jwt "<jwt>" --timeout 120
python tools/provision_farm.py --url auto --count 1 --api-url http://127.0.0.1:8787 --token "<dashboard-token>"
```

---

## 4. Token Minting and Cluster Integration

Once an account has been created and confirmed interactively in a browser session, save its `refreshToken` directly or register it into the running farm cluster via `POST /api/accounts` (include `Authorization: Bearer <token>` when dashboard auth is active):

```bash
curl -X POST http://127.0.0.1:8787/api/accounts \
  -H "Authorization: Bearer <dashboard-token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "bot_alpha", "refresh_token": "<refreshToken>"}'
```

Or offline via the CLI:

```bash
python tools/roster.py add bot_alpha --refresh-token "<refreshToken>"
```

The farm persists the credential under `$STATE_DIR/accounts/bot_alpha/refresh_token.txt` with mode `0600` and launches the supervised bot slot.
