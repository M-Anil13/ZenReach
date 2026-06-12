# Connecting Your WhatsApp Business API — Setup Guide

This guide explains exactly where to get your WhatsApp API credentials and where to add them inside the NexZen WhatsApp Campaign Suite. Total time: about 10–15 minutes the first time.

## What you will need (3 values)

The app asks for three values in **API Settings**:

1. **Permanent Access Token** — the secret key that lets the app send messages on your behalf.
2. **Phone Number ID** — the ID of the WhatsApp number messages will be sent from (this is an ID, not the phone number itself).
3. **WhatsApp Business Account ID (WABA ID)** — used to create and manage your message templates.

---

## Part 1 — One-time Meta setup

### Step 1: Create or open your Meta Business Portfolio
Go to https://business.facebook.com and sign in. If your company doesn't have a Business Portfolio yet, create one with your business name and email. This is the container that owns your WhatsApp number.

### Step 2: Create a Meta Developer App
Go to https://developers.facebook.com → **My Apps** → **Create App**. Choose the **Business** app type, give it a name (e.g., "MyCompany Campaigns"), and link it to the Business Portfolio from Step 1.

### Step 3: Add the WhatsApp product
Inside your new app's dashboard, find **WhatsApp** in the product list and click **Set up**. Meta will automatically create a WhatsApp Business Account (WABA) and give you a free test number to start with.

### Step 4: Add and verify your real business number
In **WhatsApp → API Setup**, click **Add phone number**. Enter your business display name, category, and the phone number you want to send from, then verify it with the SMS/voice code Meta sends. 
Important: a number connected to the Cloud API cannot simultaneously be used in the normal WhatsApp / WhatsApp Business mobile app.

---

## Part 2 — Where to find each value

### Phone Number ID and WABA ID
Open your app at developers.facebook.com → **WhatsApp → API Setup**. On that page you will see:
- **Phone number ID** — a long number shown directly under your selected phone number. Copy this.
- **WhatsApp Business Account ID** — shown just below it. Copy this too.

### Permanent Access Token (the important one)
The API Setup page shows a **Temporary access token — it expires in 24 hours. Do NOT use it for production.** Instead, create a permanent System User token:

1. Go to https://business.facebook.com → **Settings (gear icon) → Business Settings**.
2. In the left menu: **Users → System Users → Add**. Name it (e.g., "campaign-bot") and set the role to **Admin**.
3. Click your new system user → **Add Assets** → choose your **App** (give Full control) and your **WhatsApp Account**.
4. Click **Generate New Token** → select your app → set token expiration to **Never** → tick these two permissions:
   - `whatsapp_business_messaging`
   - `whatsapp_business_management`
5. Click **Generate Token** and copy it immediately (it starts with `EAA...`). Meta shows it only once — store it safely, like a password.

---

## Part 3 — Where to add the values in the app

1. Open the app and click **🔑 API Settings** in the left sidebar.
2. Paste the **Permanent Access Token** into the first field (use the Show/Hide button to check it).
3. Paste the **Phone Number ID** into the second field.
4. Paste the **WABA ID** into the third field.
5. Click **Save & verify**. The app makes a small test call to the Meta API; if your values are correct you'll see a green **Live mode ready** badge and the demo-mode warning banner disappears.

Each organization/workspace in the app stores its own set of keys, so different companies you onboard never share credentials. Keys are stored encrypted on the server side — never in your browser.

---

## Part 4 — Before your first real campaign (compliance checklist)

- **Templates must be approved.** Business-initiated messages must use a Meta-approved message template. Create your template (with a `{{1}}` variable for the customer name) under WhatsApp Manager → Message Templates, or from this app's Templates page. Approval usually takes minutes to a few hours.
- **Contacts must be opted in.** Only message customers who agreed to receive WhatsApp messages from you. This protects your number's quality rating.
- **Mind your messaging tier.** New numbers start with a limit of 250 unique customers per 24 hours, which automatically scales to 1K → 10K → 100K → unlimited as you send with good quality.
- **Watch your quality rating.** If many users block or report your messages, Meta can pause your templates or lower your tier. The Reports page in this app shows failure reasons so you can fix issues early.

## Troubleshooting

- **Error 190 / "Invalid OAuth access token"** — your token expired (you used the 24h temporary one) or was pasted with extra spaces. Generate a System User token (Part 2) and re-save.
- **Error 131030 / "Recipient not in allowed list"** — you're still on the test number; add recipients in API Setup, or switch to your verified business number.
- **Error 131047 / "Re-engagement message"** — more than 24 hours passed since the user's last reply, so a free-form message is blocked. Send an approved template instead (this app always sends templates for campaigns, which avoids this).
- **Messages show "sent" but never "delivered"** — the number may not be on WhatsApp, or the user's privacy settings block business messages. The campaign report flags these per contact.

---

Built by NexZen — https://nexzen.me · We Build. You Scale.
