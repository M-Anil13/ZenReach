"""No-code integrations & sync (master prompt §17d).

A generic, documented incoming-webhook framework: each integration gets a
per-org token and a field-mapping config, so Zapier / Make / Pabbly / IFTTT /
Google Sheets / WooCommerce / Shopify can push contacts and events in without
core changes. Full two-way OAuth sync (HubSpot etc.) needs each vendor's OAuth
app + review and is out of scope here; this is the inbound 90% that works today.
Plan-gated behind `integrations`.
"""
import json

from flask import Blueprint, request, jsonify, g

from .db import q, q1, ex, insert, uid, now
from .auth import require_org, require_role, require_feature, audit
from . import security as sec
from . import phones

bp = Blueprint("integrations", __name__)

KINDS = ["webhook", "zapier", "make", "google_sheets", "woocommerce", "shopify", "ad_leads"]


@bp.get("/api/integrations")
@require_org
def list_integrations():
    rows = q("SELECT id, kind, name, token, enabled, created_at FROM integrations WHERE org_id=?",
             (g.org["id"],))
    base = request.host_url.rstrip("/")
    for r in rows:
        r["inbound_url"] = base + (f"/hooks/{r['kind']}/{r['token']}" if r["kind"] in
                                   ("woocommerce", "shopify", "google_sheets")
                                   else (f"/capture/ad-lead/{r['token']}" if r["kind"] == "ad_leads"
                                         else f"/hooks/in/{r['token']}"))
    return jsonify({"integrations": rows, "kinds": KINDS})


@bp.post("/api/integrations")
@require_feature("integrations")
def create_integration():
    d = request.get_json(force=True) or {}
    kind = d.get("kind", "webhook")
    if kind not in KINDS:
        return jsonify({"error": "Unknown kind"}), 400
    iid, token = uid("int"), uid("tok")
    insert("integrations", {"id": iid, "org_id": g.org["id"], "kind": kind,
                            "name": d.get("name", kind), "token": token,
                            "config": json.dumps(d.get("config", {})), "enabled": 1, "created_at": now()})
    audit("integration.created", kind)
    base = request.host_url.rstrip("/")
    url = base + (f"/hooks/{kind}/{token}" if kind in ("woocommerce", "shopify", "google_sheets")
                  else (f"/capture/ad-lead/{token}" if kind == "ad_leads" else f"/hooks/in/{token}"))
    return jsonify({"ok": True, "id": iid, "token": token, "inbound_url": url})


@bp.delete("/api/integrations/<iid>")
@require_role("admin")
def delete_integration(iid):
    ex("DELETE FROM integrations WHERE id=? AND org_id=?", (iid, g.org["id"]))
    return jsonify({"ok": True})


# ---- inbound webhook handlers (public, token-auth) --------------------------
def _by_token(token, kind=None):
    if kind:
        return q1("SELECT * FROM integrations WHERE token=? AND kind=? AND enabled=1", (token, kind))
    return q1("SELECT * FROM integrations WHERE token=? AND enabled=1", (token,))


def _map_and_ingest(integ, data):
    """Apply the integration's field mapping, then route through lead capture."""
    cfg = json.loads(integ["config"] or "{}")
    fmap = cfg.get("map", {})   # e.g. {"phone":"billing.phone","name":"customer_name"}

    def pick(path):
        cur = data
        for part in str(path).split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur

    phone = pick(fmap.get("phone", "phone")) or data.get("phone")
    name = pick(fmap.get("name", "name")) or data.get("name") or ""
    e164 = phones.to_e164(phone, cfg.get("cc", "91"))
    if not phones.is_valid(e164):
        return None
    from .capture import _ingest_lead
    return _ingest_lead(integ["org_id"], e164, str(name), integ["kind"], data,
                        list_id=cfg.get("list_id"), journey_id=cfg.get("journey_id"),
                        sequence_id=cfg.get("sequence_id"))


@bp.post("/hooks/in/<token>")
def inbound_generic(token):
    integ = _by_token(token)
    if not integ:
        return jsonify({"error": "Invalid token"}), 401
    data = request.get_json(force=True, silent=True) or request.form.to_dict() or {}
    cid = _map_and_ingest(integ, data)
    # also fire the generic 'webhook' journey trigger
    if cid:
        from .journeys import fire_trigger
        fire_trigger(integ["org_id"], "webhook", _phone_of(cid))
    return jsonify({"ok": bool(cid)})


@bp.post("/hooks/woocommerce/<token>")
def inbound_woo(token):
    integ = _by_token(token, "woocommerce")
    if not integ:
        return jsonify({"error": "Invalid token"}), 401
    data = request.get_json(force=True, silent=True) or {}
    # WooCommerce order payload: billing.phone / billing.first_name
    billing = data.get("billing", {})
    flat = {"phone": billing.get("phone"), "name": (billing.get("first_name", "") + " " +
            billing.get("last_name", "")).strip(), "order_status": data.get("status")}
    cid = _map_and_ingest(integ, {**data, **flat})
    return jsonify({"ok": bool(cid)})


@bp.post("/hooks/shopify/<token>")
def inbound_shopify(token):
    integ = _by_token(token, "shopify")
    if not integ:
        return jsonify({"error": "Invalid token"}), 401
    data = request.get_json(force=True, silent=True) or {}
    cust = data.get("customer", data)
    flat = {"phone": cust.get("phone") or data.get("phone"),
            "name": (cust.get("first_name", "") + " " + cust.get("last_name", "")).strip()}
    cid = _map_and_ingest(integ, {**data, **flat})
    return jsonify({"ok": bool(cid)})


@bp.post("/hooks/google_sheets/<token>")
def inbound_sheets(token):
    integ = _by_token(token, "google_sheets")
    if not integ:
        return jsonify({"error": "Invalid token"}), 401
    data = request.get_json(force=True, silent=True) or request.form.to_dict() or {}
    cid = _map_and_ingest(integ, data)
    return jsonify({"ok": bool(cid)})


def _phone_of(cid):
    c = q1("SELECT phone FROM contacts WHERE id=?", (cid,))
    return (c or {}).get("phone")
