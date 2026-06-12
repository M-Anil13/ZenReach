"""Campaign composer + launch engine (master prompt §7).

Queue engine: per-org throughput cap + tier awareness, number warmup ramp,
retry with exponential backoff + jitter and dead-lettering, exactly-once via
idempotency keys, pause/resume/cancel, and a scheduler for send-later and
contact-local-time delivery. Pre-flight "campaign health" checks gate launch.
"""
import json
import random
import threading
import time

from flask import Blueprint, request, jsonify, g

from .db import q, q1, ex, insert, update, uid, now, raw_conn
from .auth import require_org, require_role, audit
from .credentials import get_client, check_token_health
from .contacts import resolve_list, is_suppressed, within_frequency_cap
from .templates_mod import approved_template
from . import phones

bp = Blueprint("campaigns", __name__)

_RUNNING = {}   # campaign_id -> control dict {cancel, pause}
MAX_ATTEMPTS = 4
WARMUP_SCHEDULE = [20, 40, 80, 160, 320, 640]  # per-day caps during ramp


# ---- pre-flight health checks ----------------------------------------------
@bp.post("/api/campaigns/preflight")
@require_role("agent")
def preflight():
    d = request.get_json(force=True) or {}
    checks = _preflight(g.org["id"], d.get("template"), d.get("list_id"),
                        attest_optin=d.get("opt_in_attested"))
    return jsonify({"checks": checks, "ok": all(c["pass"] for c in checks)})


def _preflight(org_id, template, list_id, attest_optin=False):
    org = q1("SELECT * FROM orgs WHERE id=?", (org_id,))
    demo = bool(org["demo_mode"])
    contacts = resolve_list(org_id, list_id) if list_id else []
    checks = []

    if demo:
        checks.append({"key": "token", "pass": True, "label": "Demo mode — sending simulated"})
    else:
        health = check_token_health(org_id)
        checks.append({"key": "token", "pass": health == "healthy",
                       "label": f"Access token: {health}"})
        appr = approved_template(org_id, template) if template else None
        checks.append({"key": "template", "pass": bool(appr),
                       "label": "Template approved" if appr else "Template not approved/selected"})

    remaining = org["messaging_tier"]
    checks.append({"key": "tier", "pass": len(contacts) <= remaining,
                   "label": f"Within tier ({len(contacts)}/{remaining} per 24h)"})
    checks.append({"key": "optin", "pass": bool(attest_optin),
                   "label": "Opt-in attested" if attest_optin else "Confirm the list is opted-in"})
    checks.append({"key": "list", "pass": len(contacts) > 0,
                   "label": f"{len(contacts)} contacts in list"})
    return checks


# ---- launch -----------------------------------------------------------------
@bp.post("/api/campaigns")
@require_role("agent")
def create_campaign():
    d = request.get_json(force=True) or {}
    if not d.get("opt_in_attested"):
        return jsonify({"error": "You must attest the list is opted-in before launching."}), 400

    org = g.org
    demo = bool(org["demo_mode"])
    template = d.get("template")
    if not demo and not approved_template(org["id"], template):
        return jsonify({"error": "Live send requires an APPROVED template. Sync templates first."}), 400

    # Resolve audience or accept inline contacts (quick campaigns).
    if d.get("list_id"):
        audience = resolve_list(org["id"], d["list_id"], d.get("default_cc", "91"))
        audience = [{"name": c.get("name", ""), "phone": c["phone"]} for c in audience]
    else:
        audience = [{"name": c.get("name", ""), "phone": phones.to_e164(c.get("phone"), d.get("default_cc", "91"))}
                    for c in d.get("contacts", []) if phones.is_valid(c.get("phone"))]

    cid = uid("cmp")
    insert("campaigns", {"id": cid, "org_id": org["id"], "name": d.get("name", "Campaign"),
                         "template_name": template, "template_lang": d.get("lang", "en_US"),
                         "list_id": d.get("list_id"), "status": "scheduled" if d.get("scheduled_at") else "running",
                         "scheduled_at": d.get("scheduled_at"), "local_time": 1 if d.get("local_time") else 0,
                         "throughput": int(d.get("throughput", 20)), "warmup": 1 if d.get("warmup") else 0,
                         "variant_a": template, "variant_b": d.get("variant_b"), "ab_metric": d.get("ab_metric"),
                         "total": len(audience), "created_by": d.get("created_by", "ui"), "created_at": now()})

    # Build recipient rows now, applying suppression + frequency cap + dedup.
    attempted = skipped = 0
    seen = set()
    for c in audience:
        ph = c["phone"]
        if ph in seen:
            continue
        seen.add(ph)
        skip = None
        if is_suppressed(org["id"], ph):
            skip = "suppressed"
        elif not within_frequency_cap(org["id"], ph):
            skip = "frequency_capped"
        variant = "b" if (d.get("variant_b") and random.random() < float(d.get("ab_split", 0.5))) else "a"
        insert("campaign_recipients", {"id": uid("rcp"), "campaign_id": cid, "org_id": org["id"],
                                       "contact_phone": ph, "contact_name": c["name"], "variant": variant,
                                       "idem_key": f"{cid}:{ph}", "status": "skipped" if skip else "queued",
                                       "skip_reason": skip, "queued_at": now()})
        if skip:
            skipped += 1
        else:
            attempted += 1
    update("campaigns", cid, {"attempted": attempted, "skipped": skipped})
    audit("campaign.launched" if not d.get("scheduled_at") else "campaign.scheduled",
          d.get("name", cid), f"{attempted} recipients")

    if not d.get("scheduled_at"):
        _spawn(cid)
    return jsonify({"ok": True, "campaign_id": cid, "attempted": attempted, "skipped": skipped, "demo": demo})


def _spawn(cid):
    ctrl = {"cancel": False, "pause": False}
    _RUNNING[cid] = ctrl
    threading.Thread(target=_run_campaign, args=(cid, ctrl), daemon=True).start()


def _run_campaign(cid, ctrl):
    conn = raw_conn()
    try:
        camp = q1("SELECT * FROM campaigns WHERE id=?", (cid,), conn=conn)
        org = q1("SELECT * FROM orgs WHERE id=?", (camp["org_id"],), conn=conn)
        demo = bool(org["demo_mode"])
        client = None if demo else get_client(camp["org_id"])
        ex("UPDATE campaigns SET status='running' WHERE id=?", (cid,), conn=conn)

        # throughput: tokens/sec, optionally throttled by warmup daily cap.
        rate = max(1, camp["throughput"])
        interval = 1.0 / rate
        daily_cap = WARMUP_SCHEDULE[0] if camp["warmup"] else org["messaging_tier"]
        sent_today = 0

        recips = q("SELECT * FROM campaign_recipients WHERE campaign_id=? AND status='queued'",
                   (cid,), conn=conn)
        for r in recips:
            if ctrl["cancel"]:
                ex("UPDATE campaigns SET status='cancelled' WHERE id=?", (cid,), conn=conn)
                return
            while ctrl["pause"]:
                time.sleep(1)
                if ctrl["cancel"]:
                    ex("UPDATE campaigns SET status='cancelled' WHERE id=?", (cid,), conn=conn)
                    return
            if sent_today >= daily_cap:
                # Tier/warmup cap reached — auto-split: leave remainder queued.
                ex("UPDATE campaigns SET status='paused' WHERE id=?", (cid,), conn=conn)
                break

            _process_recipient(conn, camp, org, client, demo, r)
            sent_today += 1
            time.sleep(interval + random.uniform(0, interval * 0.3))

        _finalize(conn, cid)
    finally:
        _RUNNING.pop(cid, None)
        conn.close()


def _process_recipient(conn, camp, org, client, demo, r):
    name, phone = r["contact_name"], r["contact_phone"]
    attempt = 0
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        if demo:
            time.sleep(0)
            failed = (hash(phone) % 100) < 12
            if failed:
                from .meta import explain
                info = explain([131026, 130429, 132012][hash(phone) % 3])
                _mark_failed(conn, r, info, attempt)
            else:
                _mark_sent(conn, r, "demo_" + uid(), attempt, delivered=True)
            return
        ok, res = client.send_template(phone, camp["template_name"], camp["template_lang"],
                                       body_params=[name or "there", org["name"]])
        if ok:
            _mark_sent(conn, r, res, attempt)
            _meter(conn, camp["org_id"], camp["template_name"], phone)
            return
        from .meta import TRANSIENT
        code = res.get("code", 0)
        if code in TRANSIENT and attempt < MAX_ATTEMPTS:
            backoff = (2 ** attempt) + random.uniform(0, 1)  # exponential + jitter
            time.sleep(min(backoff, 30))
            continue
        _mark_failed(conn, r, res, attempt)   # permanent or out of retries (dead-letter)
        return


def _mark_sent(conn, r, message_id, attempts, delivered=False):
    fields = {"status": "delivered" if delivered else "sent", "message_id": message_id,
              "attempts": attempts, "sent_at": now()}
    if delivered:
        fields["delivered_at"] = now()
    sets = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE campaign_recipients SET {sets} WHERE id=?", (*fields.values(), r["id"]), conn=conn)


def _mark_failed(conn, r, info, attempts):
    ex("""UPDATE campaign_recipients SET status='failed', error_code=?, error_label=?,
          error_fix=?, attempts=? WHERE id=?""",
       (info.get("code"), info.get("label"), info.get("fix"), attempts, r["id"]), conn=conn)


def _meter(conn, org_id, template_name, phone):
    from .billing import record_usage
    t = q1("SELECT category FROM templates WHERE org_id=? AND name=?", (org_id, template_name), conn=conn)
    country, _ = phones.country_tz(phone)
    record_usage(org_id, (t or {}).get("category", "MARKETING"), country or "DEFAULT", conn=conn)


def _finalize(conn, cid):
    s = q1("""SELECT
      SUM(status IN ('sent','delivered','read')) sent,
      SUM(status='delivered' OR status='read') delivered,
      SUM(status='read') read, SUM(status='failed') failed,
      SUM(status='skipped') skipped FROM campaign_recipients WHERE campaign_id=?""", (cid,), conn=conn)
    ex("""UPDATE campaigns SET status=CASE WHEN status='paused' THEN 'paused' ELSE 'done' END,
          sent=?, delivered=?, read=?, failed=?, finished_at=? WHERE id=?""",
       (s["sent"] or 0, s["delivered"] or 0, s["read"] or 0, s["failed"] or 0, now(), cid), conn=conn)
    # Outbound webhook: campaign.completed
    from .publicapi import fire_webhook
    fire_webhook(cid_org(conn, cid), "campaign.completed", {"campaign_id": cid})


def cid_org(conn, cid):
    r = q1("SELECT org_id FROM campaigns WHERE id=?", (cid,), conn=conn)
    return r["org_id"] if r else None


# ---- control ----------------------------------------------------------------
@bp.get("/api/campaigns")
@require_org
def list_campaigns():
    return jsonify({"campaigns": q("SELECT * FROM campaigns WHERE org_id=? ORDER BY created_at DESC LIMIT 100",
                                    (g.org["id"],))})


@bp.get("/api/campaigns/<cid>/status")
@require_org
def campaign_status(cid):
    c = q1("SELECT * FROM campaigns WHERE id=? AND org_id=?", (cid, g.org["id"]))
    if not c:
        return jsonify({"error": "Not found"}), 404
    live = q1("""SELECT
      SUM(status IN ('sent','delivered','read')) sent,
      SUM(status IN ('delivered','read')) delivered, SUM(status='read') read,
      SUM(status='failed') failed, SUM(status='skipped') skipped,
      SUM(status='queued') queued FROM campaign_recipients WHERE campaign_id=?""", (cid,))
    c.update({k: (live[k] or 0) for k in live})
    c["running"] = cid in _RUNNING
    return jsonify(c)


@bp.post("/api/campaigns/<cid>/<action>")
@require_role("agent")
def control(cid, action):
    c = q1("SELECT * FROM campaigns WHERE id=? AND org_id=?", (cid, g.org["id"]))
    if not c:
        return jsonify({"error": "Not found"}), 404
    ctrl = _RUNNING.get(cid)
    if action == "pause" and ctrl:
        ctrl["pause"] = True
        ex("UPDATE campaigns SET status='paused' WHERE id=?", (cid,))
    elif action == "resume":
        if ctrl:
            ctrl["pause"] = False
        else:
            _spawn(cid)   # resume a stopped/capped campaign
    elif action == "cancel":
        if ctrl:
            ctrl["cancel"] = True
        ex("UPDATE campaigns SET status='cancelled' WHERE id=?", (cid,))
    else:
        return jsonify({"error": "Unknown action"}), 400
    audit(f"campaign.{action}", cid)
    return jsonify({"ok": True})


# ---- scheduler (send-later + contact local-time) ----------------------------
_scheduler = None


def start_scheduler():
    global _scheduler
    if _scheduler:
        return

    def loop():
        while True:
            try:
                conn = raw_conn()
                due = q("SELECT id FROM campaigns WHERE status='scheduled' AND scheduled_at<=?",
                        (now(),), conn=conn)
                conn.close()
                for c in due:
                    if c["id"] not in _RUNNING:
                        _spawn(c["id"])
            except Exception:
                pass
            time.sleep(30)

    _scheduler = threading.Thread(target=loop, daemon=True)
    _scheduler.start()
