"""Visual journey / automation engine (master prompt §17b).

A journey is a graph: Trigger → (Conditions / Waits / Actions). The graph is
stored as JSON; a background worker walks each enrolled contact's run through
the nodes. The console ships a node editor; this module is the execution engine
+ CRUD + a trigger entrypoint other modules call. Plan-gated behind `journeys`.

Graph shape:
  { "entry": "n1",
    "nodes": {
      "n1": {"type":"action","action":"send_template","config":{"template":"welcome","lang":"en_US"},"next":"n2"},
      "n2": {"type":"wait","secs":86400,"next":"n3"},
      "n3": {"type":"condition","cond":"replied","yes":"n4","no":null},
      "n4": {"type":"action","action":"add_tag","config":{"tag":"engaged"},"next":null}
  }}
"""
import json
import threading
import time

from flask import Blueprint, request, jsonify, g

from .db import q, q1, ex, insert, uid, now, raw_conn
from .auth import require_org, require_role, require_feature, audit
from .contacts import is_suppressed, suppress
from .credentials import get_client

bp = Blueprint("journeys", __name__)

TRIGGERS = ["contact_added", "tag_applied", "keyword_received", "form_submitted",
            "link_clicked", "campaign_delivered", "date_anniversary", "webhook", "no_reply"]


@bp.get("/api/journeys")
@require_org
def list_journeys():
    rows = q("SELECT * FROM journeys WHERE org_id=? ORDER BY created_at DESC", (g.org["id"],))
    for j in rows:
        j["runs"] = q1("SELECT COUNT(*) c FROM journey_runs WHERE journey_id=?", (j["id"],))["c"]
    return jsonify({"journeys": rows, "triggers": TRIGGERS})


@bp.post("/api/journeys")
@require_feature("journeys")
def create_journey():
    d = request.get_json(force=True) or {}
    jid = uid("jny")
    insert("journeys", {"id": jid, "org_id": g.org["id"], "name": d.get("name", "Journey"),
                        "status": d.get("status", "draft"), "trigger_type": d.get("trigger_type"),
                        "trigger_config": json.dumps(d.get("trigger_config", {})),
                        "graph": json.dumps(d.get("graph", {"entry": None, "nodes": {}})),
                        "created_at": now()})
    audit("journey.created", d.get("name", jid))
    return jsonify({"ok": True, "id": jid})


@bp.put("/api/journeys/<jid>")
@require_feature("journeys")
def update_journey(jid):
    d = request.get_json(force=True) or {}
    fields = {}
    for k in ("name", "status", "trigger_type"):
        if k in d:
            fields[k] = d[k]
    if "trigger_config" in d:
        fields["trigger_config"] = json.dumps(d["trigger_config"])
    if "graph" in d:
        fields["graph"] = json.dumps(d["graph"])
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        ex(f"UPDATE journeys SET {sets} WHERE id=? AND org_id=?", (*fields.values(), jid, g.org["id"]))
    return jsonify({"ok": True})


@bp.get("/api/journeys/<jid>/runs")
@require_org
def journey_runs(jid):
    return jsonify({"runs": q("""SELECT contact_phone, node_id, status, started_at, updated_at
                                 FROM journey_runs WHERE journey_id=? ORDER BY updated_at DESC LIMIT 300""",
                              (jid,))})


@bp.post("/api/journeys/<jid>/enroll")
@require_feature("journeys")
def manual_enroll(jid):
    """Manually drop contacts into a journey (also used to test it)."""
    d = request.get_json(force=True) or {}
    j = q1("SELECT * FROM journeys WHERE id=? AND org_id=?", (jid, g.org["id"]))
    if not j:
        return jsonify({"error": "Not found"}), 404
    n = 0
    for c in d.get("contacts", []):
        if _enroll(g.org["id"], j, c.get("phone"), c.get("name", "")):
            n += 1
    return jsonify({"ok": True, "enrolled": n})


# ---- trigger entrypoint (called by other modules) ---------------------------
def fire_trigger(org_id, trigger_type, phone, name="", conn=None):
    """Enroll a contact into every active journey listening for this trigger."""
    own = conn is None
    c = conn or raw_conn()
    try:
        for j in q("SELECT * FROM journeys WHERE org_id=? AND status='active' AND trigger_type=?",
                   (org_id, trigger_type), conn=c):
            _enroll(org_id, j, phone, name, conn=c)
    finally:
        if own:
            c.close()


def _enroll(org_id, journey, phone, name, conn=None):
    if not phone:
        return False
    graph = json.loads(journey["graph"] or "{}")
    entry = graph.get("entry")
    if not entry:
        return False
    insert("journey_runs", {"id": uid("jrn"), "journey_id": journey["id"], "org_id": org_id,
                            "contact_phone": phone, "node_id": entry, "status": "running",
                            "log": json.dumps([]), "next_run": now(),
                            "started_at": now(), "updated_at": now()}, conn=conn)
    return True


# ---- execution worker -------------------------------------------------------
def _cond(conn, org_id, phone, cond, cfg):
    if cond == "replied":
        return q1("""SELECT 1 FROM messages m JOIN conversations c ON c.id=m.conversation_id
                     WHERE c.org_id=? AND c.contact_phone=? AND m.direction='in' LIMIT 1""",
                  (org_id, phone), conn=conn) is not None
    if cond == "clicked":
        return q1("SELECT 1 FROM short_links WHERE org_id=? AND contact_phone=? AND clicks>0 LIMIT 1",
                  (org_id, phone), conn=conn) is not None
    if cond == "opted_out":
        return is_suppressed(org_id, phone, conn=conn)
    if cond == "has_tag":
        c = q1("SELECT attrs FROM contacts WHERE org_id=? AND phone=?", (org_id, phone), conn=conn)
        return c and (cfg.get("tag") in json.loads(c["attrs"] or "{}").values())
    return False


def _do_action(conn, run, node):
    org_id, phone = run["org_id"], run["contact_phone"]
    action = node.get("action")
    cfg = node.get("config", {})
    if action == "send_template":
        org = q1("SELECT * FROM orgs WHERE id=?", (org_id,), conn=conn)
        if not org["demo_mode"]:
            client = get_client(org_id)
            if client and cfg.get("template"):
                client.send_template(phone, cfg["template"], cfg.get("lang", "en_US"),
                                     body_params=[name_of(conn, org_id, phone), org["name"]])
    elif action in ("add_tag", "remove_tag", "set_attribute"):
        c = q1("SELECT id, attrs FROM contacts WHERE org_id=? AND phone=?", (org_id, phone), conn=conn)
        if c:
            attrs = json.loads(c["attrs"] or "{}")
            if action == "add_tag":
                attrs["tag"] = cfg.get("tag")
            elif action == "remove_tag":
                attrs.pop("tag", None)
            else:
                attrs[cfg.get("key", "attr")] = cfg.get("value")
            ex("UPDATE contacts SET attrs=? WHERE id=?", (json.dumps(attrs), c["id"]), conn=conn)
    elif action == "suppress":
        suppress(org_id, phone, "journey", conn=conn)
    elif action == "add_to_sequence":
        seq = cfg.get("sequence_id")
        first = q1("SELECT delay_secs FROM sequence_steps WHERE sequence_id=? ORDER BY ord LIMIT 1",
                   (seq,), conn=conn)
        if seq and first:
            try:
                insert("sequence_enrollments", {"id": uid("enr"), "sequence_id": seq, "org_id": org_id,
                                                "contact_phone": phone, "contact_name": name_of(conn, org_id, phone),
                                                "step_idx": 0, "status": "active",
                                                "next_run": now() + int(first["delay_secs"]),
                                                "enrolled_at": now()}, conn=conn)
            except Exception:
                pass
    elif action == "webhook":
        _fire_external(cfg.get("url"), {"phone": phone, "journey_run": run["id"]})
    elif action == "assign":
        ex("UPDATE conversations SET assigned_to=? WHERE org_id=? AND contact_phone=?",
           (cfg.get("agent"), org_id, phone), conn=conn)


def name_of(conn, org_id, phone):
    c = q1("SELECT name FROM contacts WHERE org_id=? AND phone=?", (org_id, phone), conn=conn)
    return (c or {}).get("name") or "there"


def _fire_external(url, payload):
    if not url:
        return
    import requests
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception:
        pass


def _tick(conn):
    runs = q("SELECT * FROM journey_runs WHERE status='running' AND next_run<=? LIMIT 200",
             (now(),), conn=conn)
    for run in runs:
        j = q1("SELECT graph FROM journeys WHERE id=?", (run["journey_id"],), conn=conn)
        graph = json.loads((j or {}).get("graph") or "{}")
        nodes = graph.get("nodes", {})
        node = nodes.get(run["node_id"])
        if not node:
            ex("UPDATE journey_runs SET status='completed', updated_at=? WHERE id=?",
               (now(), run["id"]), conn=conn)
            continue
        t = node.get("type")
        nxt = node.get("next")
        if t == "action":
            _do_action(conn, run, node)
        elif t == "wait":
            # schedule the *next* node after the delay
            if nxt:
                ex("UPDATE journey_runs SET node_id=?, next_run=?, updated_at=? WHERE id=?",
                   (nxt, now() + int(node.get("secs", 0)), now(), run["id"]), conn=conn)
            else:
                ex("UPDATE journey_runs SET status='completed', updated_at=? WHERE id=?",
                   (now(), run["id"]), conn=conn)
            continue
        elif t == "condition":
            branch = node.get("yes") if _cond(conn, run["org_id"], run["contact_phone"],
                                              node.get("cond"), node.get("config", {})) else node.get("no")
            nxt = branch
        # advance
        if nxt:
            ex("UPDATE journey_runs SET node_id=?, next_run=?, updated_at=? WHERE id=?",
               (nxt, now(), now(), run["id"]), conn=conn)
        else:
            ex("UPDATE journey_runs SET status='completed', updated_at=? WHERE id=?",
               (now(), run["id"]), conn=conn)


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
            time.sleep(20)
    _worker = threading.Thread(target=loop, daemon=True)
    _worker.start()
