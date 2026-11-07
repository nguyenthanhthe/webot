# End-to-End Account Provisioning Pipeline: Cloudflare Temp Email + Wolvesville Farm

This document details the complete technical architecture and implementation blueprint for automated, end-to-end account provisioning for the Wolvesville Bot Farm using [`dreamhunter2333/cloudflare_temp_email`](https://github.com/dreamhunter2333/cloudflare_temp_email).

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       AUTOMATED PROVISIONING PIPELINE                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
           1. Generate Mailbox         ▼
   ┌────────────────────────────────────────────────────────┐
   │ Cloudflare Temp Email Worker (dreamhunter2333)        │
   │  POST /admin/new_address  (x-admin-auth: <secret>)     │
   │  Returns: { address, jwt }                             │
   └───────────────────────────────────┬────────────────────┘
                                       │
           2. Execute Registration     ▼
   ┌────────────────────────────────────────────────────────┐
   │ Registration Bridge (Headless Firefox / Playwright)    │
   │  • Solves Cloudflare Turnstile attestation             │
   │  • Submits { email, username, password }               │
   │  • Triggers Wolvesville verification email             │
   └───────────────────────────────────┬────────────────────┘
                                       │
           3. Poll Inbox & Verify      ▼
   ┌────────────────────────────────────────────────────────┐
   │ Verification Listener (tools/temp_email.py)            │
   │  • GET /api/parsed_mails (Authorization: Bearer <jwt>) │
   │  • Extracts verification code or activation URL        │
   │  • Completes email confirmation                        │
   └───────────────────────────────────┬────────────────────┘
                                       │
           4. Mint Refresh Token       ▼
   ┌────────────────────────────────────────────────────────┐
   │ Token Exfiltration                                     │
   │  • Calls signInWithEmailAndPassword with Cf-JWT        │
   │  • Retrieves durable refreshToken                      │
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

## 2. Cloudflare Temp Email API Specification

The `dreamhunter2333/cloudflare_temp_email` backend runs on Cloudflare Workers (using KV and D1 databases). It exposes standard JSON endpoints:

### A. Creating a New Address
- **Endpoint:** `POST https://<temp-email-worker-url>/admin/new_address`
- **Headers:**
  - `x-admin-auth: <ADMIN_PASSWORD>`
  - `Content-Type: application/json`
- **Request Body:**
  ```json
  {
    "enablePrefix": true,
    "name": "wvbot_alpha",
    "domain": "yourdomain.com"
  }
  ```
- **Response:**
  ```json
  {
    "jwt": "eyJhbGciOi...",
    "address": "wvbot_alpha@yourdomain.com"
  }
  ```

### B. Listening for Inbound Verification Emails
- **Endpoint:** `GET https://<temp-email-worker-url>/api/parsed_mails?limit=10`
- **Headers:**
  - `Authorization: Bearer <JWT>`
- **Response:**
  ```json
  [
    {
      "id": "msg_12345",
      "from": "no-reply@wolvesville.com",
      "subject": "Verify your Wolvesville email",
      "text": "Your verification code is: 489201 ...",
      "html": "<div>...</div>"
    }
  ]
  ```

---

## 3. Python Integration Helper (`tools/temp_email.py`)

The pipeline includes a zero-dependency standard library client `TempEmailClient` to interact with any deployed `cloudflare_temp_email` instance:

```python
from wvbot.tools.temp_email import TempEmailClient

client = TempEmailClient(
    base_url="https://mail.example.com",
    admin_password="your_admin_password",
)

# 1. Create a mailbox
mailbox = client.create_mailbox(prefix="bot_acc")
print(f"Created: {mailbox.address}")

# 2. Wait for incoming verification email
code = client.wait_for_verification_code(mailbox.jwt, timeout_sec=120)
print(f"Received verification code: {code}")
```

---

## 4. Token Minting and Cluster Integration

Once the account is verified, authenticate once inside the browser context to obtain the permanent `refreshToken`:

```javascript
// Inside browser page context with valid Turnstile attestation
const cfJwt = window.localStorage.getItem("cloudflare-turnstile-jwt");
const res = await fetch("https://auth.api-wolvesville.com/players/signInWithEmailAndPassword", {
  method: "POST",
  headers: { "Content-Type": "application/json", "Cf-JWT": cfJwt },
  body: JSON.stringify({ email: address, password: password }),
});
const { refreshToken } = await res.json();
```

Finally, register the bot into the running farm cluster via `POST /api/accounts`:

```bash
curl -X POST http://127.0.0.1:8787/api/accounts \
  -H "Content-Type: application/json" \
  -d '{"name": "bot_alpha", "refresh_token": "<refreshToken>"}'
```

The farm immediately persists the account under `~/.wv-bot/accounts/bot_alpha/refresh_token.txt` and hot-launches a new supervised thread.
