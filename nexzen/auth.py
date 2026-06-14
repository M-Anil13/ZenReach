"""Auth, multi-tenancy, RBAC and audit log.

Email+password (with verification), TOTP 2FA, password reset, org invitations.
Sessions via Flask signed cookies. Every tenant request resolves an active org
and a role; RBAC is enforced by decorators used across the other blueprints.
"""
import functools
from flask import Blueprint, request, jsonify, session, g

from .db import q1, q, ex, insert, uid, now
from . import security as sec

bp = Blueprint("auth", __name__)

ROLE_RANK = {"viewer": 0, "agent": 1, "admin": 2, "owner": 3}


def bootstrap_superadmin():
    """Promote NEXZEN_SUPERADMIN_EMAIL to super-admin if that user exists.
    Called once at startup so the platform operator is set by env, not by
    being the first to sign up."""
    import os
    from .db import raw_conn
    email = (os.environ.get("NEXZEN_SUPERADMIN_EMAIL") or "").strip().lower()
    if not email:
        return
    c = raw_conn()
    c.execute("UPDATE users SET is_superadmin=1 WHERE email=?", (email,))
    c.commit()
    c.close()


# ---- context resolution -----------------------------------------------------
def current_user():
    uid_ = session.get("uid")
    return q1("SELECT * FROM users WHERE id=?", (uid_,)) if uid_ else None


def current_membership():
    u = current_user()
    oid = session.get("org_id")
    if not u or not oid:
        return None
    return q1("SELECT * FROM memberships WHERE user_id=? AND org_id=?", (u["id"], oid))


def current_org():
    oid = session.get("org_id")
    return q1("SELECT * FROM orgs WHERE id=?", (oid,)) if oid else None


def audit(action, target="", meta=""):
    u = current_user()
    org = current_org()
    insert("audit_logs", {"id": uid("aud"), "org_id": org["id"] if org else None,
                          "actor": (u or {}).get("email", "system"), "action": action,
                          "target": target, "meta": meta, "created_at": now()})


# ---- decorators -------------------------------------------------------------
def require_login(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        if not current_user():
            return jsonify({"error": "Authentication required"}), 401
        return fn(*a, **k)
    return w


def require_org(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        m = current_membership()
        if not m:
            return jsonify({"error": "No active organization"}), 403
        org = current_org()
        if org and org["suspended"]:
            return jsonify({"error": "Organization suspended — contact NexZen support."}), 403
        g.membership = m
        g.org = org
        return fn(*a, **k)
    return w


def require_role(min_role):
    def deco(fn):
        @functools.wraps(fn)
        @require_org
        def w(*a, **k):
            if ROLE_RANK.get(g.membership["role"], 0) < ROLE_RANK[min_role]:
                return jsonify({"error": f"Requires {min_role} role"}), 403
            return fn(*a, **k)
        return w
    return deco


def org_feature(org_id, name):
    """True if the org's plan unlocks feature `name` (master prompt §11)."""
    import json
    row = q1("""SELECT p.features FROM orgs o JOIN plans p ON p.id=o.plan_id WHERE o.id=?""", (org_id,))
    if not row or not row["features"]:
        return False
    try:
        return bool(json.loads(row["features"]).get(name, False))
    except Exception:
        return False


def require_feature(name):
    """Gate an endpoint on a plan feature; returns 402 with an upgrade hint."""
    def deco(fn):
        @functools.wraps(fn)
        @require_org
        def w(*a, **k):
            if not org_feature(g.org["id"], name):
                return jsonify({"error": f"'{name}' is not on your plan.",
                                "upgrade": True, "feature": name}), 402
            return fn(*a, **k)
        return w
    return deco


def require_superadmin(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        u = current_user()
        if not u or not u["is_superadmin"]:
            return jsonify({"error": "Super-admin only"}), 403
        return fn(*a, **k)
    return w


# ---- routes -----------------------------------------------------------------
@bp.post("/api/auth/signup")
def signup():
    d = request.get_json(force=True) or {}
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    org_name = (d.get("org_name") or "").strip()
    category = (d.get("category") or "general").lower()
    if not email or len(pw) < 8 or not org_name:
        return jsonify({"error": "Email, 8+ char password, and organization name required."}), 400
    if q1("SELECT id FROM users WHERE email=?", (email,)):
        return jsonify({"error": "Email already registered."}), 409
    from .db import RESTRICTED_CATEGORIES
    if category in RESTRICTED_CATEGORIES:
        return jsonify({"error": f"'{category}' is prohibited by WhatsApp Commerce Policy."}), 400

    user_id, org_id = uid("usr"), uid("org")
    vtoken = uid("vt")
    # Super-admin is NOT granted to the first signup (security). It is seeded
    # only for the email in NEXZEN_SUPERADMIN_EMAIL (see bootstrap_superadmin()).
    import os
    is_super = email == (os.environ.get("NEXZEN_SUPERADMIN_EMAIL") or "").strip().lower()
    insert("users", {"id": user_id, "email": email, "name": d.get("name", email.split("@")[0]),
                     "pw_hash": sec.hash_password(pw), "email_verified": 0, "verify_token": vtoken,
                     "is_superadmin": 1 if is_super else 0, "created_at": now()})
    # Free trial window from platform config.
    import json
    from .db import q1 as _q1, PLATFORM
    tcfg = _q1("SELECT value FROM settings WHERE org_id=? AND key='trial'", (PLATFORM,))
    trial_days = (json.loads(tcfg["value"]).get("trial_days", 14) if tcfg else 14)
    insert("orgs", {"id": org_id, "name": org_name, "slug": org_name.lower().replace(" ", "-")[:40] + "-" + org_id[-4:],
                    "category": category, "plan_id": "starter", "demo_mode": 1,
                    "trial_ends": now() + trial_days * 86400, "created_at": now()})
    insert("memberships", {"id": uid("mem"), "org_id": org_id, "user_id": user_id,
                           "role": "owner", "created_at": now()})
    insert("subscriptions", {"id": uid("sub"), "org_id": org_id, "plan_id": "starter",
                             "status": "active", "created_at": now()})
    # Affiliate attribution (?ref= code captured at signup).
    ref = (d.get("ref") or request.args.get("ref") or "").strip()
    if ref:
        try:
            from .affiliates import attribute_signup
            attribute_signup(ref, org_id)
        except Exception:
            pass
    session["uid"] = user_id
    session["org_id"] = org_id
    audit("org.created", org_id, org_name)
    # PRODUCTION: email this link. Returned here so the flow is testable.
    return jsonify({"ok": True, "user_id": user_id, "org_id": org_id,
                    "verify_link": f"/api/auth/verify?token={vtoken}",
                    "superadmin": bool(is_super)})


@bp.get("/api/auth/verify")
def verify_email():
    tok = request.args.get("token", "")
    u = q1("SELECT * FROM users WHERE verify_token=?", (tok,))
    if not u:
        return jsonify({"error": "Invalid verification token"}), 400
    ex("UPDATE users SET email_verified=1, verify_token=NULL WHERE id=?", (u["id"],))
    return jsonify({"ok": True, "message": "Email verified."})


@bp.post("/api/auth/login")
def login():
    d = request.get_json(force=True) or {}
    email = (d.get("email") or "").strip().lower()
    u = q1("SELECT * FROM users WHERE email=?", (email,))
    if not u or not sec.verify_password(d.get("password", ""), u["pw_hash"] or ""):
        return jsonify({"error": "Invalid email or password."}), 401
    if u["totp_enabled"]:
        if not d.get("totp"):
            return jsonify({"need_2fa": True}), 200
        if not sec.totp_verify(u["totp_secret"], d.get("totp")):
            return jsonify({"error": "Invalid 2FA code."}), 401
    session["uid"] = u["id"]
    m = q1("SELECT * FROM memberships WHERE user_id=? ORDER BY created_at LIMIT 1", (u["id"],))
    if m:
        session["org_id"] = m["org_id"]
    audit("user.login", u["email"])
    return jsonify({"ok": True, "superadmin": bool(u["is_superadmin"])})


@bp.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.get("/api/auth/me")
def me():
    u = current_user()
    if not u:
        return jsonify({"authenticated": False})
    orgs = q("""SELECT o.id, o.name, o.demo_mode, o.plan_id, m.role FROM orgs o
                JOIN memberships m ON m.org_id=o.id WHERE m.user_id=?""", (u["id"],))
    return jsonify({"authenticated": True, "user": {"id": u["id"], "email": u["email"],
                    "name": u["name"], "verified": bool(u["email_verified"]),
                    "totp": bool(u["totp_enabled"]), "superadmin": bool(u["is_superadmin"])},
                    "orgs": orgs, "active_org": session.get("org_id")})


@bp.post("/api/auth/switch-org")
@require_login
def switch_org():
    oid = (request.get_json(force=True) or {}).get("org_id")
    if not q1("SELECT id FROM memberships WHERE user_id=? AND org_id=?", (session["uid"], oid)):
        return jsonify({"error": "Not a member of that org"}), 403
    session["org_id"] = oid
    return jsonify({"ok": True})


# ---- 2FA --------------------------------------------------------------------
@bp.post("/api/auth/2fa/setup")
@require_login
def twofa_setup():
    u = current_user()
    secret = sec.new_totp_secret()
    ex("UPDATE users SET totp_secret=? WHERE id=?", (secret, u["id"]))
    return jsonify({"secret": secret, "otpauth": sec.otpauth_uri(secret, u["email"])})


@bp.post("/api/auth/2fa/enable")
@require_login
def twofa_enable():
    u = current_user()
    if not sec.totp_verify(u["totp_secret"], (request.get_json(force=True) or {}).get("code")):
        return jsonify({"error": "Invalid code"}), 400
    ex("UPDATE users SET totp_enabled=1 WHERE id=?", (u["id"],))
    audit("user.2fa_enabled", u["email"])
    return jsonify({"ok": True})


# ---- password reset ---------------------------------------------------------
@bp.post("/api/auth/reset/request")
def reset_request():
    email = ((request.get_json(force=True) or {}).get("email") or "").strip().lower()
    u = q1("SELECT * FROM users WHERE email=?", (email,))
    if u:
        tok = uid("rst")
        ex("UPDATE users SET reset_token=?, reset_expires=? WHERE id=?", (tok, now() + 3600, u["id"]))
        from .mailer import send_email, smtp_configured
        emailed = send_email(
            email, "Reset your NexZen password",
            f"<p>Use this code to reset your password (valid 1 hour):</p>"
            f"<p style='font-size:18px;font-weight:bold'>{tok}</p>"
            f"<p>Paste it on the password-reset screen.</p>")
        # Token returned only when email isn't configured, so the flow still works.
        return jsonify({"ok": True, "emailed": emailed,
                        **({} if emailed else {"reset_token": tok})})
    return jsonify({"ok": True, "emailed": False})  # do not leak existence


@bp.post("/api/auth/reset/confirm")
def reset_confirm():
    d = request.get_json(force=True) or {}
    u = q1("SELECT * FROM users WHERE reset_token=?", (d.get("token"),))
    if not u or (u["reset_expires"] or 0) < now():
        return jsonify({"error": "Invalid or expired token"}), 400
    if len(d.get("password", "")) < 8:
        return jsonify({"error": "Password too short"}), 400
    ex("UPDATE users SET pw_hash=?, reset_token=NULL WHERE id=?",
       (sec.hash_password(d["password"]), u["id"]))
    return jsonify({"ok": True})


# ---- invites + members ------------------------------------------------------
@bp.post("/api/members/invite")
@require_role("admin")
def invite():
    d = request.get_json(force=True) or {}
    email = (d.get("email") or "").strip().lower()
    role = d.get("role", "agent")
    if role not in ROLE_RANK:
        return jsonify({"error": "Invalid role"}), 400
    tok = uid("inv")
    insert("invites", {"id": uid("inv"), "org_id": g.org["id"], "email": email,
                       "role": role, "token": tok, "created_at": now()})
    audit("member.invited", email, role)

    join_url = request.host_url.rstrip("/") + "/join?token=" + tok
    from .mailer import send_email, smtp_configured
    cfg = org_smtp(g.org["id"])
    org_name = g.org["name"]
    emailed = send_email(
        email, f"You're invited to {org_name} on NexZen",
        f"<p>You've been invited to join <b>{org_name}</b> as <b>{role}</b> on the NexZen "
        f"WhatsApp Campaign Suite.</p><p><a href='{join_url}'>Click here to accept</a> "
        f"(log in or sign up first with this email).</p><p>Or paste this link: {join_url}</p>",
        cfg=cfg)
    return jsonify({"ok": True, "invite_link": join_url, "emailed": emailed,
                    "smtp": smtp_configured(cfg)})


# ---- profile self-service ---------------------------------------------------
@bp.post("/api/auth/profile")
@require_login
def update_profile():
    d = request.get_json(force=True) or {}
    u = current_user()
    fields = {}
    if d.get("name"):
        fields["name"] = d["name"].strip()
    if d.get("new_password"):
        if not sec.verify_password(d.get("current_password", ""), u["pw_hash"] or ""):
            return jsonify({"error": "Current password is incorrect."}), 400
        if len(d["new_password"]) < 8:
            return jsonify({"error": "New password must be 8+ characters."}), 400
        fields["pw_hash"] = sec.hash_password(d["new_password"])
    if not fields:
        return jsonify({"error": "Nothing to update."}), 400
    sets = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), u["id"]))
    audit("user.profile_updated", u["email"], "password" if "pw_hash" in fields else "name")
    return jsonify({"ok": True})


# ---- per-org SMTP (owner) ---------------------------------------------------
def org_smtp(org_id):
    row = q1("SELECT value FROM settings WHERE org_id=? AND key='smtp'", (org_id,))
    if not row:
        return None
    import json
    try:
        return json.loads(row["value"])
    except Exception:
        return None


@bp.get("/api/settings/smtp")
@require_role("owner")
def get_smtp():
    cfg = org_smtp(g.org["id"]) or {}
    from .mailer import smtp_configured
    return jsonify({"configured": smtp_configured(cfg) or smtp_configured(),
                    "user": cfg.get("user", ""), "server": cfg.get("server", "smtp.hostinger.com"),
                    "port": cfg.get("port", "465"), "source": "org" if cfg else "env_or_none"})


@bp.post("/api/settings/smtp")
@require_role("owner")
def set_smtp():
    import json
    d = request.get_json(force=True) or {}
    cfg = json.dumps({"user": d.get("user", ""), "pass": d.get("pass", ""),
                      "server": d.get("server", "smtp.hostinger.com"), "port": str(d.get("port", "465"))})
    ex("INSERT INTO settings VALUES (?,?,?) ON CONFLICT(org_id,key) DO UPDATE SET value=?",
       (g.org["id"], "smtp", cfg, cfg))
    audit("settings.smtp_updated", g.org["id"])
    return jsonify({"ok": True})


def _accept(token, user):
    inv = q1("SELECT * FROM invites WHERE token=? AND accepted=0", (token,))
    if not inv:
        return None
    if not q1("SELECT id FROM memberships WHERE org_id=? AND user_id=?", (inv["org_id"], user["id"])):
        insert("memberships", {"id": uid("mem"), "org_id": inv["org_id"], "user_id": user["id"],
                               "role": inv["role"], "created_at": now()})
    ex("UPDATE invites SET accepted=1 WHERE id=?", (inv["id"],))
    session["org_id"] = inv["org_id"]
    return inv


@bp.post("/api/members/accept")
@require_login
def accept_invite():
    inv = _accept((request.get_json(force=True) or {}).get("token"), current_user())
    return (jsonify({"ok": True}) if inv else (jsonify({"error": "Invalid invite"}), 400))


@bp.get("/join")
def join_page():
    """Clickable invite landing. Accepts if logged in, else points to sign-in."""
    from flask import redirect
    tok = request.args.get("token", "")
    u = current_user()
    if u and _accept(tok, u):
        return redirect("/console")
    # Not logged in (or already used): send to console; user logs in with the
    # invited email, then re-clicks the emailed link to join.
    return redirect("/console?join=" + tok)


@bp.get("/api/members")
@require_role("admin")
def list_members():
    rows = q("""SELECT u.email, u.name, m.role, m.created_at FROM memberships m
                JOIN users u ON u.id=m.user_id WHERE m.org_id=?""", (g.org["id"],))
    return jsonify({"members": rows})


@bp.get("/api/audit")
@require_role("admin")
def audit_log():
    return jsonify({"events": q("SELECT * FROM audit_logs WHERE org_id=? ORDER BY created_at DESC LIMIT 200",
                                (g.org["id"],))})
