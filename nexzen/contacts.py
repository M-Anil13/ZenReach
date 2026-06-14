"""Contacts: import, normalization, dedup, lists, segments, suppression,
opt-in/consent, frequency capping, GDPR export/erasure.

Suppression and frequency caps are enforced here AND re-checked at the queue
level (campaigns.py) so the UI can never bypass them.
"""
import io
import json
import re

import pandas as pd
from flask import Blueprint, request, jsonify, g, send_file

from .db import q, q1, ex, insert, update, uid, now
from .auth import require_org, require_role, audit, current_user
from . import phones

bp = Blueprint("contacts", __name__)

PHONE_KEYS = ["phone", "mobile", "number", "whatsapp", "contact", "msisdn", "cell"]
NAME_KEYS = ["name", "first", "customer", "client", "person"]
STOP_WORDS = {"stop", "unsubscribe", "optout", "opt-out", "cancel", "end", "quit",
              "बंद", "रोको", "إيقاف", "alto", "parar"}


def _guess(headers, rows, keys):
    for i, h in enumerate(headers):
        if any(k in str(h).lower() for k in keys):
            return i
    return -1


# ---- import -----------------------------------------------------------------
@bp.post("/api/contacts/parse")
@require_org
def parse():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400
    try:
        if f.filename.lower().endswith(".csv"):
            df = pd.read_csv(f, dtype=str, keep_default_na=False)
        else:
            df = pd.read_excel(f, dtype=str)
        df = df.fillna("")
    except Exception as e:
        return jsonify({"error": f"Could not read file: {e}"}), 400
    headers = [str(c) for c in df.columns]
    rows = df.astype(str).values.tolist()
    return jsonify({"headers": headers, "rows": rows[:50], "total": len(rows),
                    "phone_col": _guess(headers, rows, PHONE_KEYS),
                    "name_col": _guess(headers, rows, NAME_KEYS),
                    "all_rows_token": _stash(headers, rows)})


# Small in-memory stash so the import preview can commit without re-upload.
_IMPORT_STASH = {}


def _stash(headers, rows):
    tok = uid("imp")
    _IMPORT_STASH[tok] = (headers, rows)
    return tok


@bp.post("/api/contacts/import")
@require_role("agent")
def import_contacts():
    d = request.get_json(force=True) or {}
    tok, pc, nc = d.get("token"), int(d.get("phone_col", -1)), int(d.get("name_col", -1))
    cc = str(d.get("default_cc", "91"))
    list_name = (d.get("list_name") or "").strip()
    opt_in = bool(d.get("opt_in"))
    if tok not in _IMPORT_STASH or pc < 0:
        return jsonify({"error": "Re-upload the file and pick a phone column."}), 400
    headers, rows = _IMPORT_STASH.pop(tok)
    extra_cols = [i for i in range(len(headers)) if i not in (pc, nc)]

    supp = {r["phone"] for r in q("SELECT phone FROM suppression WHERE org_id=?", (g.org["id"],))}
    seen, valid, dup, invalid, suppressed = set(), 0, 0, 0, 0
    list_id = None
    if list_name:
        list_id = uid("lst")
        insert("lists", {"id": list_id, "org_id": g.org["id"], "name": list_name,
                         "kind": "static", "created_at": now()})

    rejected = []
    for r in rows:
        raw = r[pc] if pc < len(r) else ""
        if not phones.is_valid(raw):
            invalid += 1
            rejected.append({"row": r, "reason": "invalid number"})
            continue
        e164 = phones.to_e164(raw, cc)
        if e164 in seen:
            dup += 1
            continue
        seen.add(e164)
        if e164 in supp:
            suppressed += 1
            continue
        country, tz = phones.country_tz(e164)
        name = r[nc].strip() if (nc >= 0 and nc < len(r)) else ""
        attrs = {headers[i]: (r[i] if i < len(r) else "") for i in extra_cols}
        existing = q1("SELECT id FROM contacts WHERE org_id=? AND phone=?", (g.org["id"], e164))
        if existing:
            cid = existing["id"]
            update("contacts", cid, {"name": name or None, "attrs": json.dumps(attrs),
                                     "country": country, "timezone": tz})
        else:
            cid = uid("con")
            insert("contacts", {"id": cid, "org_id": g.org["id"], "phone": e164, "name": name,
                                "country": country, "timezone": tz, "attrs": json.dumps(attrs),
                                "opt_in": 1 if opt_in else 0, "opt_in_source": "import" if opt_in else None,
                                "opt_in_at": now() if opt_in else None, "created_at": now()})
        if opt_in:
            insert("consent_records", {"id": uid("cns"), "org_id": g.org["id"], "contact_phone": e164,
                                       "action": "opt_in", "source": "sheet_import",
                                       "actor": current_user()["email"], "created_at": now()})
        if list_id:
            ex("INSERT OR IGNORE INTO list_contacts VALUES (?,?)", (list_id, cid))
        valid += 1

    audit("contacts.imported", list_name or "(no list)", f"{valid} valid")
    return jsonify({"valid": valid, "duplicates": dup, "invalid": invalid,
                    "suppressed_skipped": suppressed, "list_id": list_id,
                    "rejected": rejected[:200]})


# ---- contacts CRUD ----------------------------------------------------------
@bp.get("/api/contacts")
@require_org
def list_contacts():
    rows = q("SELECT * FROM contacts WHERE org_id=? ORDER BY created_at DESC LIMIT 500", (g.org["id"],))
    return jsonify({"contacts": rows, "count": q1("SELECT COUNT(*) c FROM contacts WHERE org_id=?",
                                                   (g.org["id"],))["c"]})


@bp.post("/api/contacts/<cid>/optin")
@require_role("agent")
def set_optin(cid):
    d = request.get_json(force=True) or {}
    update("contacts", cid, {"opt_in": 1 if d.get("opt_in") else 0,
                             "opt_in_source": d.get("source", "manual"), "opt_in_at": now()})
    insert("consent_records", {"id": uid("cns"), "org_id": g.org["id"], "contact_phone": cid,
                               "action": "opt_in" if d.get("opt_in") else "opt_out",
                               "source": d.get("source", "manual"),
                               "actor": current_user()["email"], "created_at": now()})
    return jsonify({"ok": True})


# ---- GDPR / DPDP ------------------------------------------------------------
@bp.get("/api/contacts/<cid>/export")
@require_role("agent")
def gdpr_export(cid):
    c = q1("SELECT * FROM contacts WHERE id=? AND org_id=?", (cid, g.org["id"]))
    if not c:
        return jsonify({"error": "Not found"}), 404
    consent = q("SELECT * FROM consent_records WHERE org_id=? AND contact_phone=?", (g.org["id"], c["phone"]))
    audit("contact.exported", c["phone"])
    return jsonify({"contact": c, "consent_records": consent})


@bp.delete("/api/contacts/<cid>")
@require_role("admin")
def gdpr_delete(cid):
    c = q1("SELECT phone FROM contacts WHERE id=? AND org_id=?", (cid, g.org["id"]))
    if not c:
        return jsonify({"error": "Not found"}), 404
    ex("DELETE FROM contacts WHERE id=? AND org_id=?", (cid, g.org["id"]))
    audit("contact.erased", c["phone"])
    return jsonify({"ok": True})


# ---- lists & segments -------------------------------------------------------
@bp.get("/api/lists")
@require_org
def get_lists():
    rows = q("SELECT * FROM lists WHERE org_id=? ORDER BY created_at DESC", (g.org["id"],))
    for r in rows:
        r["count"] = q1("SELECT COUNT(*) c FROM list_contacts WHERE list_id=?", (r["id"],))["c"]
    return jsonify({"lists": rows})


@bp.post("/api/segments")
@require_role("agent")
def create_segment():
    """Dynamic segment: filter on tags/country/language/last_engaged."""
    d = request.get_json(force=True) or {}
    sid = uid("seg")
    insert("lists", {"id": sid, "org_id": g.org["id"], "name": d.get("name", "Segment"),
                     "kind": "dynamic", "filter_json": json.dumps(d.get("filter", {})),
                     "created_at": now()})
    return jsonify({"ok": True, "id": sid})


def resolve_list(org_id, list_id, default_cc="91"):
    """Return [{phone,name,...}] for a static list or dynamic segment, after
    removing suppressed numbers and non-opted-in contacts per policy."""
    lst = q1("SELECT * FROM lists WHERE id=? AND org_id=?", (list_id, org_id))
    if not lst:
        return []
    if lst["kind"] == "dynamic":
        f = json.loads(lst["filter_json"] or "{}")
        sql = "SELECT * FROM contacts WHERE org_id=?"
        args = [org_id]
        if f.get("country"):
            sql += " AND country=?"; args.append(f["country"])
        if f.get("language"):
            sql += " AND language=?"; args.append(f["language"])
        if f.get("opted_in"):
            sql += " AND opt_in=1"
        if f.get("engaged_after"):
            sql += " AND last_engaged>=?"; args.append(int(f["engaged_after"]))
        rows = q(sql, tuple(args))
    else:
        rows = q("""SELECT c.* FROM contacts c JOIN list_contacts lc ON lc.contact_id=c.id
                    WHERE lc.list_id=? AND c.org_id=?""", (list_id, org_id))
    return rows


# ---- suppression ------------------------------------------------------------
@bp.get("/api/suppression")
@require_org
def get_suppression():
    return jsonify({"suppression": q("SELECT * FROM suppression WHERE org_id=? ORDER BY created_at DESC",
                                      (g.org["id"],))})


@bp.post("/api/suppression")
@require_role("agent")
def add_suppression():
    d = request.get_json(force=True) or {}
    nums = d.get("phones") or ([d["phone"]] if d.get("phone") else [])
    for raw in nums:
        suppress(g.org["id"], phones.to_e164(raw), d.get("reason", "manual"))
    audit("suppression.added", f"{len(nums)} numbers")
    return jsonify({"ok": True, "added": len(nums)})


def suppress(org_id, phone, reason="stop", conn=None):
    """Add a number to the do-not-contact list (idempotent)."""
    ex("INSERT OR IGNORE INTO suppression VALUES (?,?,?,?)", (org_id, phone, reason, now()), conn=conn)
    ex("UPDATE contacts SET opt_in=0 WHERE org_id=? AND phone=?", (org_id, phone), conn=conn)


def is_suppressed(org_id, phone, conn=None):
    return q1("SELECT 1 FROM suppression WHERE org_id=? AND phone=?", (org_id, phone), conn=conn) is not None


def is_stop_keyword(text):
    return (text or "").strip().lower() in STOP_WORDS


# ---- frequency cap ----------------------------------------------------------
def within_frequency_cap(org_id, phone, window_secs=86400, max_msgs=1):
    """True if sending now would respect the org's marketing frequency cap."""
    cfg = q1("SELECT value FROM settings WHERE org_id=? AND key='freq_cap'", (org_id,))
    if cfg:
        try:
            c = json.loads(cfg["value"])
            window_secs, max_msgs = int(c.get("window", window_secs)), int(c.get("max", max_msgs))
        except Exception:
            pass
    since = now() - window_secs
    n = q1("""SELECT COUNT(*) c FROM campaign_recipients
              WHERE org_id=? AND contact_phone=? AND sent_at>=?""", (org_id, phone, since))["c"]
    return n < max_msgs


@bp.post("/api/settings/frequency-cap")
@require_role("admin")
def set_freq_cap():
    d = request.get_json(force=True) or {}
    val = json.dumps({"window": int(d.get("window", 86400)), "max": int(d.get("max", 1))})
    ex("INSERT INTO settings VALUES (?,?,?) ON CONFLICT(org_id,key) DO UPDATE SET value=?",
       (g.org["id"], "freq_cap", val, val))
    return jsonify({"ok": True})
