import os
import re
import uuid
import threading
import smtplib
import ssl
import io
import base64
import json as _json
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
jobs = {}

_FONT_PATHS = [
    "C:/Windows/Fonts/ariblk.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(size):
    for path in _FONT_PATHS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def stamp_name(template_bytes, name, y_pct):
    img  = Image.open(io.BytesIO(template_bytes)).convert("RGBA")
    draw = ImageDraw.Draw(img)
    W, H = img.size
    text  = name.strip().upper()
    max_w = W * 0.72
    fs    = int(H * 0.075)
    font  = _load_font(fs)
    while fs > 16:
        bb = draw.textbbox((0, 0), text, font=font)
        if (bb[2] - bb[0]) <= max_w:
            break
        fs   -= 2
        font  = _load_font(fs)
    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    x   = (W - tw) / 2
    y   = H * (y_pct / 100.0) - th / 2
    pad = int(th * 0.7)
    draw.rectangle([int(W * 0.02), int(y - pad), int(W * 0.98), int(y + th + pad)],
                   fill=(255, 255, 255, 255))
    draw.text((x, y), text, fill=(0, 0, 0, 255), font=font)
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    out.seek(0)
    return out


def _build_html(body_template, student_row, reg_link, body_type='plain'):
    """Replace [col] placeholders then convert to HTML (plain) or send raw (html)."""
    body = body_template.strip()
    # Replace column placeholders in both modes
    for col, val in student_row.items():
        body = body.replace(f'[{col}]', str(val) if val else '')
    if reg_link:
        link_html = f'<a href="{reg_link}" style="color:#1a3a5c;font-weight:bold;">{reg_link}</a>'
        body = body.replace('[Insert Link]', link_html)
    else:
        body = body.replace('[Insert Link]', '')

    if body_type == 'html':
        # User pasted raw HTML — send as-is, no conversion
        return body

    # Plain text mode: *bold* → <strong>, double-newline → paragraphs
    body = re.sub(r'\*(.+?)\*', r'<strong>\1</strong>', body)
    html_parts = []
    for para in body.split('\n\n'):
        para = para.strip()
        if para:
            html_parts.append(f'<p>{para.replace(chr(10), "<br>")}</p>')
    return (
        '<!DOCTYPE html><html>'
        '<body style="font-family:Arial,sans-serif;line-height:1.8;'
        'margin:0;padding:0;color:#222;">'
        + ''.join(html_parts)
        + '</body></html>'
    )


def _send_job(job_id, students, template_bytes, docs_list,
              reg_link, cc_email, y_pct, body_template,
              attach_type, name_col, subject, body_type='plain'):
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

    # CC: split comma-separated emails from frontend input
    cc_list = [e.strip() for e in cc_email.split(',') if e.strip()] if cc_email else []

    for s in students:
        raw_email    = str(s.get('email', '')).strip()
        # Email column may contain multiple comma-separated addresses
        to_list      = [e.strip() for e in raw_email.split(',') if e.strip()]
        display_name = str(s.get(name_col) or s.get('name') or raw_email).strip()

        if not to_list:
            state['failed'] += 1
            state['errors'].append(f"Skipped — no email for {display_name}")
            state['report'].append({'Name': display_name, 'Email': '', 'Status': 'Failed', 'Reason': 'No email'})
            continue

        try:
            msg            = MIMEMultipart()
            msg['From']    = EMAIL_USER
            msg['To']      = ', '.join(to_list)
            msg['Cc']      = ', '.join(cc_list)
            msg['Subject'] = subject or "Mail from Spirit AI Solutions"
            msg['Date']    = formatdate(localtime=True)

            msg.attach(MIMEText(_build_html(body_template, s, reg_link, body_type), 'html'))

            if attach_type in ('certificate', 'both') and template_bytes:
                cert_io   = stamp_name(template_bytes, display_name, y_pct)
                safe      = ''.join(c if c.isalnum() or c in ' _-' else '_' for c in display_name)
                part      = MIMEBase('application', 'octet-stream')
                part.set_payload(cert_io.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="Certificate_{safe}.png"')
                msg.attach(part)

            if attach_type in ('docs', 'both') and docs_list:
                for fname, fbytes in docs_list:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(fbytes)
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="{fname}"')
                    msg.attach(part)

            server.sendmail(EMAIL_USER, to_list + cc_list, msg.as_string())
            state['sent'] += 1
            state['report'].append({'Name': display_name, 'Email': ', '.join(to_list), 'Status': 'Sent', 'Reason': ''})

        except Exception as e:
            state['failed'] += 1
            state['errors'].append(f"{display_name} ({', '.join(to_list)}): {e}")
            state['report'].append({'Name': display_name, 'Email': ', '.join(to_list), 'Status': 'Failed', 'Reason': str(e)})

    try:
        server.quit()
    except Exception:
        pass
    state['done'] = True


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/parse-excel', methods=['POST'])
def parse_excel():
    f = request.files.get('excel')
    if not f:
        return jsonify({'error': 'No file'}), 400
    try:
        df = pd.read_excel(f)
        df.columns = df.columns.str.strip().str.lower()
        columns = df.columns.tolist()
        sample  = {k: str(v) for k, v in df.iloc[0].to_dict().items()} if len(df) > 0 else {}
        return jsonify({'columns': columns, 'sample': sample, 'total': len(df)})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/preview-email', methods=['POST'])
def preview_email():
    body_template = request.form.get('body_template', '')
    reg_link      = request.form.get('reg_link', '')
    y_pct         = float(request.form.get('y_pct', 44))
    attach_type   = request.form.get('attach_type', 'none')
    name_col      = request.form.get('name_col', 'name')
    sample_row    = _json.loads(request.form.get('sample_row', '{}'))

    body_type = request.form.get('body_type', 'plain')
    html      = _build_html(body_template, sample_row, reg_link, body_type)
    result = {'html': html, 'cert_img': None}

    cert_file = request.files.get('certificate')
    if cert_file and attach_type in ('certificate', 'both'):
        name            = str(sample_row.get(name_col) or 'Student').strip()
        cert_io         = stamp_name(cert_file.read(), name, y_pct)
        result['cert_img'] = base64.b64encode(cert_io.read()).decode()

    return jsonify(result)


@app.route('/preview', methods=['POST'])
def preview():
    cert_file = request.files.get('certificate')
    name      = request.form.get('name', 'Student Name')
    y_pct     = float(request.form.get('y_pct', 44))
    if not cert_file:
        return 'No certificate file', 400
    return send_file(stamp_name(cert_file.read(), name, y_pct), mimetype='image/png')


@app.route('/send', methods=['POST'])
def send():
    try:
        excel_file    = request.files['excel']
        reg_link      = request.form.get('reg_link', '').strip()
        cc_email      = request.form.get('cc_email', '').strip()
        y_pct         = float(request.form.get('y_pct', 44))
        body_template = request.form.get('body_template', '').strip()
        attach_type   = request.form.get('attach_type', 'none')
        name_col      = request.form.get('name_col', 'name')
        subject       = request.form.get('subject', '').strip()
        body_type     = request.form.get('body_type', 'plain')

        if not body_template:
            return jsonify({'error': 'Email body is empty.'}), 400

        df = pd.read_excel(excel_file)
        df.columns = df.columns.str.strip().str.lower()

        if 'email' not in df.columns:
            return jsonify({'error': f"'email' column required. Found: {df.columns.tolist()}"}), 400

        students = [
            {k: str(v) for k, v in s.items()}
            for s in df.dropna(subset=['email']).to_dict('records')
        ]

        template_bytes = None
        if attach_type in ('certificate', 'both'):
            cf = request.files.get('certificate')
            if cf:
                template_bytes = cf.read()

        docs_list = []
        if attach_type in ('docs', 'both'):
            for f in request.files.getlist('docs'):
                docs_list.append((f.filename, f.read()))

        job_id = uuid.uuid4().hex
        threading.Thread(
            target=_send_job,
            args=(job_id, students, template_bytes, docs_list,
                  reg_link, cc_email, y_pct, body_template,
                  attach_type, name_col, subject, body_type),
            daemon=True
        ).start()

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
        return 'Not ready', 400
    df_all = pd.DataFrame(
        job.get('report', []),
        columns=['Name', 'Email', 'Status', 'Reason']
    )
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        df_all[df_all['Status'] == 'Sent'].drop(columns=['Reason']).to_excel(
            writer, sheet_name='Sent', index=False)
        df_all[df_all['Status'] == 'Failed'].to_excel(
            writer, sheet_name='Failed', index=False)
    out.seek(0)
    return send_file(
        out,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='Email_Report.xlsx'
    )


if __name__ == '__main__':
    print('=' * 50)
    print('  Spirit AI Mailer')
    print('  Open http://localhost:5000 in browser')
    print('=' * 50)
    app.run(debug=False, port=5000)
