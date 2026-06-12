import os
import re
import uuid
import threading
import smtplib
import ssl
import io
import base64
import json as _json
import time
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify, send_file, redirect, send_from_directory
import requests
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formatdate

app  = Flask(__name__)
jobs = {}


# ─── NexZen WhatsApp Campaign Suite wiring ───────────────────────────────────
def _flask_secret():
    env = os.environ.get("NEXZEN_FLASK_SECRET")
    if env:
        return env
    path = os.path.join(os.path.dirname(__file__), ".flask_secret")
    if os.path.exists(path):
        return open(path).read()
    s = uuid.uuid4().hex + uuid.uuid4().hex
    with open(path, "w") as fh:
        fh.write(s)
    return s


app.secret_key = _flask_secret()

from nexzen.config import load_env
load_env()   # populate os.environ from .env before anything reads config

from nexzen import db as nxdb
from nexzen.auth import bp as auth_bp
from nexzen.contacts import bp as contacts_bp
from nexzen.credentials import bp as credentials_bp, start_health_monitor
from nexzen.templates_mod import bp as templates_bp
from nexzen.campaigns import bp as campaigns_bp, start_scheduler
from nexzen.webhooks import bp as webhooks_bp
from nexzen.inbox import bp as inbox_bp
from nexzen.reports import bp as reports_bp
from nexzen.billing import bp as billing_bp
from nexzen.publicapi import bp as publicapi_bp
from nexzen.ai import bp as ai_bp
from nexzen.admin import bp as admin_bp

for _bp in (auth_bp, contacts_bp, credentials_bp, templates_bp, campaigns_bp, webhooks_bp,
            inbox_bp, reports_bp, billing_bp, publicapi_bp, ai_bp, admin_bp):
    app.register_blueprint(_bp)

app.teardown_appcontext(nxdb.close_db)
nxdb.init_db()
from nexzen.auth import bootstrap_superadmin
bootstrap_superadmin()

_bg_started = False


def _start_background():
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    start_scheduler()
    start_health_monitor()
    from nexzen.admin import start_trial_enforcer
    start_trial_enforcer()


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
              attach_type, name_col, subject, body_type,
              smtp_user, smtp_pass, smtp_server, smtp_port, delay=3.0):
    state = {
        'sent': 0, 'failed': 0, 'total': len(students),
        'done': False, 'paused': False, 'errors': [], 'report': []
    }
    jobs[job_id] = state

    try:
        ctx    = ssl.create_default_context()
        server = smtplib.SMTP_SSL(smtp_server, int(smtp_port), context=ctx)
        server.login(smtp_user, smtp_pass)
    except Exception as e:
        state['errors'].append(f"SMTP connect failed: {e}")
        state['done'] = True
        return

    cc_list = [e.strip() for e in cc_email.split(',') if e.strip()] if cc_email else []

    # Pre-generate all certificates in parallel to avoid blocking during send
    cert_cache = {}
    if attach_type in ('certificate', 'both') and template_bytes:
        def _gen_cert(s):
            name = str(s.get(name_col) or s.get('name') or '').strip()
            if not name:
                return name, None
            buf = stamp_name(template_bytes, name, y_pct)
            return name, buf.read()

        with ThreadPoolExecutor(max_workers=min(8, len(students))) as ex:
            for name, data in ex.map(_gen_cert, students):
                if data:
                    cert_cache[name] = data

    for s in students:
        if state.get('paused'):
            break

        raw_email    = str(s.get('email', '')).strip()
        to_list      = [e.strip() for e in raw_email.split(',') if e.strip()]
        display_name = str(s.get(name_col) or s.get('name') or raw_email).strip()

        if not to_list:
            state['failed'] += 1
            state['errors'].append(f"Skipped — no email for {display_name}")
            state['report'].append({'Name': display_name, 'Email': '', 'Status': 'Failed', 'Reason': 'No email'})
            continue

        try:
            msg            = MIMEMultipart()
            msg['From']    = smtp_user
            msg['To']      = ', '.join(to_list)
            msg['Cc']      = ', '.join(cc_list)
            msg['Subject'] = subject or "Mail from NexZen"
            msg['Date']    = formatdate(localtime=True)

            msg.attach(MIMEText(_build_html(body_template, s, reg_link, body_type), 'html'))

            if attach_type in ('certificate', 'both') and template_bytes:
                cert_data = cert_cache.get(display_name)
                if cert_data:
                    safe = ''.join(c if c.isalnum() or c in ' _-' else '_' for c in display_name)
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(cert_data)
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

            # Smart retry: if rate-limited (421/452), wait 30s and retry once
            try:
                server.sendmail(smtp_user, to_list + cc_list, msg.as_string())
            except smtplib.SMTPResponseException as smtp_err:
                if smtp_err.smtp_code in (421, 452):
                    state['errors'].append(f"Rate limited — waiting 30s then retrying {display_name}...")
                    time.sleep(30)
                    server.sendmail(smtp_user, to_list + cc_list, msg.as_string())
                else:
                    raise

            state['sent'] += 1
            state['report'].append({'Name': display_name, 'Email': ', '.join(to_list), 'Status': 'Sent', 'Reason': ''})
            time.sleep(delay)

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
def home():
    # Minimal launcher — full home page comes later.
    return render_template('home.html')


@app.route('/mail')
def mail():
    return render_template('index.html')


@app.route('/whatsapp')
def whatsapp():
    # Static SPA — bypass Jinja so the JSX `{{ }}` syntax isn't parsed as templating.
    return send_from_directory('templates', 'whatsapp.html')


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
        smtp_user     = request.form.get('smtp_user', '').strip()
        smtp_pass     = request.form.get('smtp_pass', '')
        smtp_server   = request.form.get('smtp_server', 'smtp.hostinger.com').strip()
        smtp_port     = request.form.get('smtp_port', '465').strip()
        delay         = float(request.form.get('delay', 3.0))
        send_limit    = request.form.get('send_limit', '').strip()

        if not body_template:
            return jsonify({'error': 'Email body is empty.'}), 400
        if not smtp_user or not smtp_pass:
            return jsonify({'error': 'Sender email and password are required.'}), 400

        df = pd.read_excel(excel_file)
        df.columns = df.columns.str.strip().str.lower()

        if 'email' not in df.columns:
            return jsonify({'error': f"'email' column required. Found: {df.columns.tolist()}"}), 400

        students = [
            {k: str(v) for k, v in s.items()}
            for s in df.dropna(subset=['email']).to_dict('records')
        ]
        if send_limit and send_limit.isdigit():
            students = students[:int(send_limit)]

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
                  attach_type, name_col, subject, body_type,
                  smtp_user, smtp_pass, smtp_server, smtp_port, delay),
            daemon=True
        ).start()

        return jsonify({'job_id': job_id, 'total': len(students)})

    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/status/<job_id>')
def status(job_id):
    return jsonify(jobs.get(job_id, {'error': 'Job not found'}))


@app.route('/pause/<job_id>', methods=['POST'])
def pause_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    job['paused'] = True
    job['done']   = True
    return jsonify({'ok': True})


@app.route('/report/<job_id>')
def report(job_id):
    job = jobs.get(job_id)
    if not job:
        return 'Job not found', 404
    if not job.get('report'):
        return 'No data yet', 400
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


# ─── WhatsApp Campaign Suite ─────────────────────────────────────────────────

GRAPH_VER = 'v21.0'
GRAPH     = f'https://graph.facebook.com/{GRAPH_VER}'

# In-memory store. Single-tenant for now; per-org store is the production path.
wa_creds = {'token': '', 'phone_id': '', 'waba_id': ''}
wa_jobs  = {}

# Meta error code → plain-English label + fix (admin-editable in production).
WA_ERRORS = {
    131026: ('Number not registered on WhatsApp', 'Remove or verify the number with the customer.'),
    131047: ('Re-engagement window closed', 'More than 24h since last reply — send an approved template (campaigns already do).'),
    131048: ('Spam rate limit hit', 'Slow down. Pause and resume after a cooldown; keep quality high.'),
    130429: ('Messaging tier limit reached', 'Pause and resume after 24h, or raise your tier by maintaining quality rating.'),
    131056: ('Pair rate limit', 'Too many messages to this number in a short window — retry later.'),
    132000: ('Template param mismatch', 'Number of {{n}} variables sent does not match the approved template.'),
    132001: ('Template does not exist / not approved', 'Check the template name + language; submit and get it approved in Meta.'),
    132012: ('Template paused by Meta (low quality)', 'Edit the template copy and resubmit for approval.'),
    190:    ('Invalid / expired access token', 'Generate a permanent System User token (the 24h temporary one expired).'),
    131030: ('Recipient not in allowed list', 'You are still on the test number — add recipients or switch to your verified number.'),
    100:    ('Invalid parameter', 'Check Phone Number ID and request fields.'),
}


def _wa_error(code, fallback='Send failed'):
    label, fix = WA_ERRORS.get(int(code or 0), (fallback, 'Check the Meta error code and retry.'))
    return {'code': code, 'label': label, 'fix': fix}


def _clean_phone(raw):
    raw = str(raw or '').strip()
    p = re.sub(r'[^\d+]', '', raw)
    if p.startswith('00'):
        p = '+' + p[2:]
    return p


def _to_e164(raw, cc):
    p = _clean_phone(raw)
    if p.startswith('+'):
        return p
    d = re.sub(r'\D', '', p)
    if not d:
        return ''
    if len(d) <= 10 and cc:
        d = str(cc) + d
    return '+' + d


def _valid_phone(raw):
    d = re.sub(r'\D', '', _clean_phone(raw))
    return 8 <= len(d) <= 15


@app.route('/whatsapp/verify', methods=['POST'])
def wa_verify():
    data     = request.get_json(force=True) or {}
    token    = (data.get('token') or '').strip()
    phone_id = (data.get('phone_id') or '').strip()
    waba_id  = (data.get('waba_id') or '').strip()
    if not token or not phone_id:
        return jsonify({'ok': False, 'error': 'Access token and Phone Number ID are required.'}), 400
    try:
        r = requests.get(
            f'{GRAPH}/{phone_id}',
            params={'fields': 'verified_name,display_phone_number,quality_rating'},
            headers={'Authorization': f'Bearer {token}'},
            timeout=15,
        )
        j = r.json()
        if r.status_code == 200:
            wa_creds.update({'token': token, 'phone_id': phone_id, 'waba_id': waba_id})
            return jsonify({
                'ok': True,
                'name': j.get('verified_name'),
                'number': j.get('display_phone_number'),
                'quality': j.get('quality_rating'),
            })
        err  = (j.get('error') or {})
        info = _wa_error(err.get('code'), err.get('message', 'Verification failed'))
        return jsonify({'ok': False, 'error': info['label'], 'fix': info['fix'], 'code': info['code']}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Could not reach Meta API: {e}'}), 502


@app.route('/whatsapp/templates')
def wa_templates():
    """List approved templates for the connected WABA."""
    if not (wa_creds['token'] and wa_creds['waba_id']):
        return jsonify({'templates': []})
    try:
        r = requests.get(
            f'{GRAPH}/{wa_creds["waba_id"]}/message_templates',
            params={'fields': 'name,status,language,category', 'limit': 200},
            headers={'Authorization': f'Bearer {wa_creds["token"]}'},
            timeout=15,
        )
        data = (r.json() or {}).get('data', [])
        return jsonify({'templates': data})
    except Exception as e:
        return jsonify({'templates': [], 'error': str(e)})


def _wa_send_one(to, template, lang, name):
    """Send one approved template message. Returns (ok, error_dict_or_msgid)."""
    components = []
    if name:
        components = [{'type': 'body', 'parameters': [{'type': 'text', 'text': name}]}]
    payload = {
        'messaging_product': 'whatsapp',
        'to': to.lstrip('+'),
        'type': 'template',
        'template': {
            'name': template,
            'language': {'code': lang},
            **({'components': components} if components else {}),
        },
    }
    try:
        r = requests.post(
            f'{GRAPH}/{wa_creds["phone_id"]}/messages',
            json=payload,
            headers={'Authorization': f'Bearer {wa_creds["token"]}'},
            timeout=20,
        )
        j = r.json()
        if r.status_code == 200 and j.get('messages'):
            return True, j['messages'][0].get('id')
        err = (j.get('error') or {})
        return False, _wa_error(err.get('code'), err.get('message', 'Send failed'))
    except Exception as e:
        return False, _wa_error(0, str(e))


def _wa_job(job_id, contacts, template, lang, demo, delay):
    state = wa_jobs[job_id]
    for c in contacts:
        if state.get('cancelled'):
            break
        name, phone = c.get('name', ''), c.get('phone', '')
        if demo:
            time.sleep(delay)
            failed = (hash(phone) % 100) < 12  # ~12% simulated failure
            if failed:
                info = _wa_error([131026, 130429, 132012][hash(phone) % 3])
                state['failed'] += 1
                state['rows'].append({'name': name, 'phone': phone, 'status': 'failed', 'reason': info})
            else:
                state['delivered'] += 1
                state['rows'].append({'name': name, 'phone': phone, 'status': 'delivered', 'reason': None})
        else:
            ok, res = _wa_send_one(phone, template, lang, name)
            if ok:
                state['delivered'] += 1
                state['rows'].append({'name': name, 'phone': phone, 'status': 'sent', 'reason': None, 'mid': res})
            else:
                state['failed'] += 1
                state['rows'].append({'name': name, 'phone': phone, 'status': 'failed', 'reason': res})
            time.sleep(delay)
        state['done'] += 1
    # Simulated read-rate for demo only (real read needs webhooks).
    if demo:
        state['read'] = int(state['delivered'] * 0.62)
    state['finished'] = True


@app.route('/whatsapp/send', methods=['POST'])
def wa_send():
    data     = request.get_json(force=True) or {}
    contacts = data.get('contacts', [])          # [{name, phone}], already E.164 from client
    template = (data.get('template') or '').strip()
    lang     = (data.get('lang') or 'en_US').strip()
    cc       = data.get('cc', '91')
    delay    = float(data.get('delay', 1.0))

    # Normalize + drop invalids server-side (defence in depth).
    clean, skipped = [], []
    for c in contacts:
        if _valid_phone(c.get('phone')):
            clean.append({'name': c.get('name', ''), 'phone': _to_e164(c.get('phone'), cc)})
        else:
            skipped.append(c)

    demo = not (wa_creds['token'] and wa_creds['phone_id'])
    if not demo and not template:
        return jsonify({'error': 'Approved template name is required for live sending.'}), 400

    job_id = uuid.uuid4().hex
    wa_jobs[job_id] = {
        'total': len(contacts), 'attempted': len(clean), 'skipped': len(skipped),
        'done': 0, 'delivered': 0, 'failed': 0, 'read': 0,
        'rows': [], 'skipped_rows': skipped, 'finished': False, 'cancelled': False,
        'demo': demo, 'template': template,
    }
    threading.Thread(
        target=_wa_job,
        args=(job_id, clean, template, lang, demo, max(0.05, delay)),
        daemon=True,
    ).start()
    return jsonify({'job_id': job_id, 'attempted': len(clean), 'skipped': len(skipped), 'demo': demo})


@app.route('/whatsapp/status/<job_id>')
def wa_status(job_id):
    return jsonify(wa_jobs.get(job_id, {'error': 'Job not found'}))


@app.route('/whatsapp/cancel/<job_id>', methods=['POST'])
def wa_cancel(job_id):
    job = wa_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    job['cancelled'] = True
    return jsonify({'ok': True})


@app.route('/console')
def console():
    # Authenticated multi-tenant console (full suite). Static SPA — bypass Jinja.
    return send_from_directory('templates', 'console.html')


@app.route('/api')
def api_index():
    """Lightweight API map for verification + docs."""
    routes = sorted({str(r.rule) for r in app.url_map.iter_rules()
                     if str(r.rule).startswith(('/api', '/v1', '/webhooks', '/l/'))})
    return jsonify({"service": "NexZen WhatsApp Campaign Suite", "version": "2.0.0",
                    "endpoints": routes})


_start_background()

if __name__ == '__main__':
    print('=' * 56)
    print('  NexZen Suite')
    print('  Home     ->  http://localhost:8080/')
    print('  Console  ->  http://localhost:8080/console   (full suite)')
    print('  WhatsApp ->  http://localhost:8080/whatsapp  (quick demo)')
    print('  Mail     ->  http://localhost:8080/mail')
    print('  API map  ->  http://localhost:8080/api')
    print('=' * 56)
    app.run(debug=False, host='0.0.0.0', port=8080)
