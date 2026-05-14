import os
import re
import uuid
import threading
import smtplib
import ssl
import io
from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formatdate

# ─── Hardcoded credentials ────────────────────────────────────────────────────
EMAIL_USER  = "sales@spiritaisolutions.com"
EMAIL_PASS  = "Hyndhavi@5172"
SMTP_SERVER = "smtp.hostinger.com"
SMTP_PORT   = 465
# ─────────────────────────────────────────────────────────────────────────────

app  = Flask(__name__)
jobs = {}  # job_id → progress state dict

_FONT_PATHS = [
    "C:/Windows/Fonts/ariblk.ttf",   # Arial Black (heaviest, best match)
    "C:/Windows/Fonts/arialbd.ttf",  # Arial Bold
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_PATHS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def stamp_name(template_bytes: bytes, name: str, y_pct: float) -> io.BytesIO:
    """Erase placeholder name then stamp student name at given y%."""
    img  = Image.open(io.BytesIO(template_bytes)).convert("RGBA")
    draw = ImageDraw.Draw(img)
    W, H = img.size

    text  = name.strip().upper()
    max_w = W * 0.72

    # Match original certificate font size (~7.5% of height), shrink only if too wide
    fs   = int(H * 0.075)
    font = _load_font(fs)
    while fs > 16:
        bb = draw.textbbox((0, 0), text, font=font)
        if (bb[2] - bb[0]) <= max_w:
            break
        fs  -= 2
        font = _load_font(fs)

    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    x = (W - tw) / 2
    y = H * (y_pct / 100.0) - th / 2

    # Erase existing placeholder name: paint solid white over that row
    pad = int(th * 0.7)
    draw.rectangle(
        [int(W * 0.02), int(y - pad), int(W * 0.98), int(y + th + pad)],
        fill=(255, 255, 255, 255)
    )

    draw.text((x, y), text, fill=(0, 0, 0, 255), font=font)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    out.seek(0)
    return out


def _email_body(body_template: str, name: str, reg_link: str) -> str:
    """Convert plain-text template → HTML. Replaces [Student] and [Insert Link]."""
    body = body_template.strip()
    body = body.replace('[Student]', name)
    body = body.replace('[Insert Link]', f'<a href="{reg_link}" style="color:#1a3a5c;font-weight:bold;">{reg_link}</a>')
    # *text* → <strong>text</strong>
    body = re.sub(r'\*(.+?)\*', r'<strong>\1</strong>', body)
    # Build HTML paragraphs from double-newline blocks
    paragraphs = body.split('\n\n')
    html_parts = []
    for para in paragraphs:
        para = para.strip()
        if para:
            html_parts.append(f'<p>{para.replace(chr(10), "<br>")}</p>')
    html_content = '\n'.join(html_parts)
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;line-height:1.8;max-width:620px;margin:auto;padding:24px;color:#222;">
{html_content}
</body>
</html>"""


def _send_job(job_id, students, template_bytes, reg_link, cc_email, y_pct, body_template):
    state = {
        'sent': 0, 'failed': 0, 'total': len(students),
        'done': False, 'errors': [], 'report': []
    }
    jobs[job_id] = state

    try:
        ctx    = ssl.create_default_context()
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=ctx)
        server.login(EMAIL_USER, EMAIL_PASS)
    except Exception as e:
        state['errors'].append(f"SMTP connect failed: {e}")
        state['done'] = True
        return

    cc_list = [cc_email.strip()] if cc_email and cc_email.strip() else []

    for s in students:
        name  = str(s.get('name', '')).strip()
        email = str(s.get('email', '')).strip()

        if not name or not email:
            state['failed'] += 1
            state['errors'].append("Skipped row — empty name or email")
            state['report'].append({'Name': name, 'Email': email, 'Status': 'Failed', 'Reason': 'Empty name or email'})
            continue

        try:
            cert_io = stamp_name(template_bytes, name, y_pct)

            msg            = MIMEMultipart()
            msg['From']    = EMAIL_USER
            msg['To']      = email
            msg['Cc']      = ", ".join(cc_list)
            msg['Subject'] = "Your Participation Certificate – Spirit AI Solutions Workshop"
            msg['Date']    = formatdate(localtime=True)

            msg.attach(MIMEText(_email_body(body_template, name, reg_link), 'html'))

            safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in name)
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(cert_io.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition',
                            f'attachment; filename="Certificate_{safe_name}.png"')
            msg.attach(part)

            server.sendmail(EMAIL_USER, [email] + cc_list, msg.as_string())
            state['sent'] += 1
            state['report'].append({'Name': name, 'Email': email, 'Status': 'Sent', 'Reason': ''})

        except Exception as e:
            state['failed'] += 1
            state['errors'].append(f"{name} ({email}): {e}")
            state['report'].append({'Name': name, 'Email': email, 'Status': 'Failed', 'Reason': str(e)})

    try:
        server.quit()
    except Exception:
        pass

    state['done'] = True


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/preview', methods=['POST'])
def preview():
    """Return stamped certificate PNG so user can verify name position."""
    cert_file = request.files.get('certificate')
    name      = request.form.get('name', 'Student Name')
    y_pct     = float(request.form.get('y_pct', 44))
    if not cert_file:
        return 'No certificate file provided', 400
    out = stamp_name(cert_file.read(), name, y_pct)
    return send_file(out, mimetype='image/png')


@app.route('/send', methods=['POST'])
def send():
    try:
        excel_file     = request.files['excel']
        cert_file      = request.files['certificate']
        reg_link       = request.form.get('reg_link', '').strip()
        cc_email       = request.form.get('cc_email', '').strip()
        y_pct          = float(request.form.get('y_pct', 44))
        body_template  = request.form.get('body_template', '').strip()

        df = pd.read_excel(excel_file)
        df.columns = df.columns.str.strip().str.lower()

        missing = [c for c in ('name', 'email') if c not in df.columns]
        if missing:
            return jsonify({
                'error': f"Missing columns: {missing}. Columns found: {df.columns.tolist()}"
            }), 400

        students       = df[['name', 'email']].dropna(subset=['email']).to_dict('records')
        template_bytes = cert_file.read()
        job_id         = uuid.uuid4().hex

        if not body_template:
            return jsonify({'error': 'Email body is empty.'}), 400

        t = threading.Thread(
            target=_send_job,
            args=(job_id, students, template_bytes, reg_link, cc_email, y_pct, body_template),
            daemon=True
        )
        t.start()

        return jsonify({'job_id': job_id, 'total': len(students)})

    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/status/<job_id>')
def status(job_id):
    return jsonify(jobs.get(job_id, {'error': 'Job not found'}))


@app.route('/report/<job_id>')
def report(job_id):
    job = jobs.get(job_id)
    if not job:
        return 'Job not found', 404
    if not job.get('done'):
        return 'Report not ready yet', 400

    rows    = job.get('report', [])
    df_all  = pd.DataFrame(rows, columns=['Name', 'Email', 'Status', 'Reason'])
    df_sent = df_all[df_all['Status'] == 'Sent'].drop(columns=['Reason'])
    df_fail = df_all[df_all['Status'] == 'Failed']

    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        df_sent.to_excel(writer, sheet_name='Sent',   index=False)
        df_fail.to_excel(writer, sheet_name='Failed', index=False)
    out.seek(0)

    return send_file(
        out,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='Email_Report.xlsx'
    )


if __name__ == '__main__':
    print("=" * 50)
    print("  Spirit AI Certificate Mailer")
    print("  Open http://localhost:5000 in browser")
    print("=" * 50)
    app.run(debug=False, port=5000)
