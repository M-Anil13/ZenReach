# Spirit AI – Mail Marketing Web App

A Flask-based bulk email platform for sending personalized emails with certificates and attachments to students or colleges.

---

## Features

- Upload student/contact list via Excel — no hardcoding
- Auto-detects columns → click to insert as placeholders in email body
- Attachment options: **None / Certificate / Docs / Both**
- Personalized certificate generation — student name stamped on PNG template per recipient
- Email body supports **Plain Text** (`*bold*`, `[placeholder]`) or raw **HTML**
- Preview email for first student before sending
- Live progress tracker — sent / failed counts in real time
- Download report as Excel (Sent sheet + Failed sheet)
- Multiple comma-separated emails supported in To and CC fields
- SMTP via Hostinger

---

## Tech Stack

- Python 3.10+
- Flask
- pandas + openpyxl
- Pillow (certificate image generation)
- smtplib (Hostinger SMTP SSL)

---

## Project Structure

```
maildraft/
├── app.py                  # Flask backend
├── requirements.txt
└── templates/
    └── index.html          # Web UI
```

---

## Installation

```bash
git clone https://github.com/M-Anil13/mail_marketing.git
cd mail_marketing
pip install -r requirements.txt
python app.py
```

Open browser → `http://localhost:5000`

---

## Excel Format

| name | email | any_column |
|------|-------|------------|
| Rahul Kumar | rahul@gmail.com | MIT College |
| Priya Sharma | priya@gmail.com, cc@gmail.com | IIT Delhi |

- `email` column is **mandatory**
- Multiple emails per cell: separate with comma
- All other columns become available as `[column_name]` placeholders in body

---

## How It Works

1. **Step 1** — Upload Excel → columns auto-parsed → click chip to insert `[col]` in body
2. **Step 2** — Choose attachment: None / Certificate / Docs / Both (all optional)
3. **Step 3** — Set subject, CC, registration link, write email body (plain or HTML)
4. **Preview** — See rendered email for first student before sending
5. **Send** — Emails sent with personalized certificate/docs attached
6. **Report** — Download Excel with Sent and Failed sheets

---

## SMTP Configuration

Credentials are set in `app.py`:

```python
EMAIL_USER  = "your_email@domain.com"
EMAIL_PASS  = "your_password"
SMTP_SERVER = "smtp.hostinger.com"
SMTP_PORT   = 465
```

---

## Author

**Spirit AI Solutions Pvt Ltd**  
+91 63055 31544 | sales@spiritaisolutions.com  
https://spiritaisolutions.com/
