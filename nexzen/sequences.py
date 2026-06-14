"""Drip / sequence marketing (master prompt §17a).

Ordered template steps with per-step delays; enroll a contact/list/segment;
branch-or-exit on behavior (replied / clicked / opted out); a background worker
advances due enrollments, respecting suppression + opt-in at every send.
Plan-gated behind the `sequences` feature.
"""
import threading
import time

from flask import Blueprint, request, jsonify, g

from .db import q, q1, ex, insert, uid, now, raw_conn
from .auth import require_org, require_role, require_feature, audit
from .contacts import resolve_list, is_suppressed
from .credentials import get_client

bp = Blueprint("sequences", __name__)


@bp.get("/api/sequences")
@require_org
def list_sequences():
    rows = q("SELECT * FROM sequences WHERE org_id=? ORDER BY created_at DESC", (g.org["id"],))
    for s in rows:
        s["steps"] = q("SELECT * FROM sequence_steps WHERE sequence_id=? ORDER BY ord", (s["id"],))
        s["enrolled"] = q1("SELECT COUNT(*) c FROM sequence_enrollments WHERE sequence_id=?", (s["id"],))["c"]
    return jsonify({"sequences": rows})


@bp.post("/api/sequences")
@require_feature("sequences")
def create_sequence():
    d = request.get_json(force=True) or {}
    sid = uid("seq")
    insert("sequences", {"id": sid, "org_id": g.org["id"], "name": d.get("name", "Sequence"),
                         "status": "active", "created_at": now()})
    for i, st in enumerate(d.get("steps", [])):
        insert("sequence_steps", {"id": uid("sst"), "sequence_id": sid, "org_id": g.org["id"],
                                  "ord": i, "delay_secs": int(st.get("delay_secs", 0)),
                                  "template_name": st.get("template"), "lang": st.get("lang", "en_US"),
                                  "exit_on": st.get("exit_on")})
    audit("sequence.created", d.get("name", sid))
    return jsonify({"ok": True, "id": sid})


@bp.post("/api/sequences/<sid>/enroll")
@require_feature("sequences")
def enroll(sid):
    """Enroll a list/segment or explicit contacts; re-enrollment prevented."""
    d = request.get_json(force=True) or {}
    seq = q1("SELECT * FROM sequences WHERE id=? AND org_id=?", (sid, g.org["id"]))
    if not seq:
        return jsonify({"error": "Not found"}), 404
    first = q1("SELECT delay_secs FROM sequence_steps WHERE sequence_id=? ORDER BY ord LIMIT 1", (sid,))
    if not first:
        return jsonify({"error": "Sequence has no steps"}), 400

    if d.get("list_id"):
        contacts = [{"name": c.get("name", ""), "phone": c["phone"]}
                    for c in resolve_list(g.org["id"], d["list_id"])]
    else:
        contacts = d.get("contacts", [])

    n = 0
    for c in contacts:
        ph = c.get("phone")
        if not ph or is_suppressed(g.org["id"], ph):
            continue
        try:
            insert("sequence_enrollments", {"id": uid("enr"), "sequence_id": sid, "org_id": g.org["id"],
                                            "contact_phone": ph, "contact_name": c.get("name", ""),
                                            "step_idx": 0, "status": "active",
                                            "next_run": now() + int(first["delay_secs"]),
                                            "enrolled_at": now()})
            n += 1
        except Exception:
            pass   # UNIQUE(sequence_id, contact_phone) prevents re-enrollment
    return jsonify({"ok": True, "enrolled": n})


@bp.get("/api/sequences/<sid>/analytics")
@require_org
def analytics(sid):
    steps = q("SELECT * FROM sequence_steps WHERE sequence_id=? ORDER BY ord", (sid,))
    funnel = []
    for st in steps:
        reached = q1("""SELECT COUNT(*) c FROM sequence_enrollments
                        WHERE sequence_id=? AND step_idx>?""", (sid, st["ord"]))["c"]
        funnel.append({"ord": st["ord"], "template": st["template_name"], "passed": reached})
    status = q("""SELECT status, COUNT(*) c FROM sequence_enrollments WHERE sequence_id=? GROUP BY status""", (sid,))
    return jsonify({"funnel": funnel, "status": {r["status"]: r["c"] for r in status}})


@bp.post("/api/sequences/<sid>/<action>")
@require_role("agent")
def control(sid, action):
    if action in ("pause", "resume"):
        ex("UPDATE sequences SET status=? WHERE id=? AND org_id=?",
           ("paused" if action == "pause" else "active", sid, g.org["id"]))
        return jsonify({"ok": True})
    return jsonify({"error": "Unknown action"}), 400


# ---- worker -----------------------------------------------------------------
def _exited(conn, org_id, phone, cond):
    """Behavioral branch/exit checks before sending the next step."""
    if cond == "opted_out":
        return is_suppressed(org_id, phone, conn=conn)
    if cond == "replied":
        return q1("""SELECT 1 FROM messages m JOIN conversations c ON c.id=m.conversation_id
                     WHERE c.org_id=? AND c.contact_phone=? AND m.direction='in' LIMIT 1""",
                  (org_id, phone), conn=conn) is not None
    if cond == "clicked":
        return q1("SELECT 1 FROM short_links WHERE org_id=? AND contact_phone=? AND clicks>0 LIMIT 1",
                  (org_id, phone), conn=conn) is not None
    return False


def _tick(conn):
    due = q("""SELECT e.*, s.status seq_status FROM sequence_enrollments e
               JOIN sequences s ON s.id=e.sequence_id
               WHERE e.status='active' AND e.next_run<=? LIMIT 200""", (now(),), conn=conn)
    for e in due:
        if e["seq_status"] != "active":
            continue
        steps = q("SELECT * FROM sequence_steps WHERE sequence_id=? ORDER BY ord",
                  (e["sequence_id"],), conn=conn)
        if e["step_idx"] >= len(steps):
            ex("UPDATE sequence_enrollments SET status='completed' WHERE id=?", (e["id"],), conn=conn)
            continue
        step = steps[e["step_idx"]]
        if step["exit_on"] and _exited(conn, e["org_id"], e["contact_phone"], step["exit_on"]):
            ex("UPDATE sequence_enrollments SET status='exited' WHERE id=?", (e["id"],), conn=conn)
            continue
        if is_suppressed(e["org_id"], e["contact_phone"], conn=conn):
            ex("UPDATE sequence_enrollments SET status='exited' WHERE id=?", (e["id"],), conn=conn)
            continue
        # send this step's template
        org = q1("SELECT * FROM orgs WHERE id=?", (e["org_id"],), conn=conn)
        if not org["demo_mode"]:
            client = get_client(e["org_id"])
            if client and step["template_name"]:
                client.send_template(e["contact_phone"], step["template_name"], step["lang"],
                                     body_params=[e["contact_name"] or "there", org["name"]])
        # advance to next step
        nxt = e["step_idx"] + 1
        if nxt >= len(steps):
            ex("UPDATE sequence_enrollments SET step_idx=?, status='completed' WHERE id=?",
               (nxt, e["id"]), conn=conn)
        else:
            ex("UPDATE sequence_enrollments SET step_idx=?, next_run=? WHERE id=?",
               (nxt, now() + int(steps[nxt]["delay_secs"]), e["id"]), conn=conn)


_worker = None


def start_worker():
    global _worker
    if _worker:
        return

    def loop():
        while True:
            try:
                conn = raw_conn()
                _tick(conn)
                conn.close()
            except Exception:
                pass
            time.sleep(30)
    _worker = threading.Thread(target=loop, daemon=True)
    _worker.start()
