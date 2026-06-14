"""SQLite data layer — full schema for the NexZen WhatsApp Campaign Suite.

Deliverable #1 of the master prompt: orgs, users, memberships, credentials,
contacts, lists, segments, suppression, consent_records, templates,
template_variants, campaigns, campaign_recipients, message_events,
conversations, messages, plans, subscriptions, usage_records, api_keys,
audit_logs, exports — plus webhook_events and an idempotency ledger.

Every tenant-owned row carries org_id; all data-access helpers force an
org_id predicate so cross-org leakage is structurally hard.
"""
import os
import sqlite3
import time
import uuid
from flask import g

DB_PATH = os.environ.get("NEXZEN_DB", os.path.join(os.path.dirname(__file__), "..", "nexzen.db"))

# Ensure the parent directory exists (e.g. /data on a mounted volume) so SQLite
# can create the file instead of failing with "unable to open database file".
_dbdir = os.path.dirname(os.path.abspath(DB_PATH))
if _dbdir:
    os.makedirs(_dbdir, exist_ok=True)


def uid(prefix=""):
    return (prefix + uuid.uuid4().hex)[:32] if not prefix else prefix + "_" + uuid.uuid4().hex[:24]


def now():
    return int(time.time())


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def close_db(_e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Standalone connection for background threads (no Flask request context).
def raw_conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    return c


# ---- thin query helpers (org-scoping enforced by callers) -------------------
def q(sql, args=(), conn=None):
    cur = (conn or get_db()).execute(sql, args)
    return [dict(r) for r in cur.fetchall()]


def q1(sql, args=(), conn=None):
    cur = (conn or get_db()).execute(sql, args)
    r = cur.fetchone()
    return dict(r) if r else None


def ex(sql, args=(), conn=None):
    db = conn or get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def insert(table, data, conn=None):
    cols = ", ".join(data.keys())
    ph = ", ".join("?" for _ in data)
    db = conn or get_db()
    db.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", tuple(data.values()))
    db.commit()
    return data.get("id")


def update(table, id_, data, conn=None):
    sets = ", ".join(f"{k}=?" for k in data)
    db = conn or get_db()
    db.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*data.values(), id_))
    db.commit()


SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT UNIQUE,
  category TEXT, plan_id TEXT DEFAULT 'starter',
  white_label_logo TEXT, white_label_accent TEXT, custom_domain TEXT,
  messaging_tier INTEGER DEFAULT 250, quality_rating TEXT DEFAULT 'UNKNOWN',
  verification_status TEXT DEFAULT 'unverified',
  suspended INTEGER DEFAULT 0, demo_mode INTEGER DEFAULT 1,
  created_at INTEGER
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT,
  pw_hash TEXT, email_verified INTEGER DEFAULT 0, verify_token TEXT,
  reset_token TEXT, reset_expires INTEGER,
  totp_secret TEXT, totp_enabled INTEGER DEFAULT 0,
  is_superadmin INTEGER DEFAULT 0, created_at INTEGER
);

CREATE TABLE IF NOT EXISTS memberships (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, user_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'agent',   -- owner|admin|agent|viewer
  created_at INTEGER,
  UNIQUE(org_id, user_id),
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS invites (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, email TEXT NOT NULL,
  role TEXT NOT NULL, token TEXT UNIQUE, accepted INTEGER DEFAULT 0,
  created_at INTEGER, FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

-- WhatsApp credentials, secrets stored AES-256-GCM encrypted at rest.
CREATE TABLE IF NOT EXISTS credentials (
  org_id TEXT PRIMARY KEY, token_enc TEXT, phone_id TEXT, waba_id TEXT,
  verified_name TEXT, display_number TEXT, last_health TEXT, last_checked INTEGER,
  graph_version TEXT DEFAULT 'v21.0', updated_at INTEGER,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS contacts (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, phone TEXT NOT NULL,
  name TEXT, language TEXT, country TEXT, timezone TEXT, attrs TEXT,
  opt_in INTEGER DEFAULT 0, opt_in_source TEXT, opt_in_at INTEGER,
  last_engaged INTEGER, created_at INTEGER,
  UNIQUE(org_id, phone),
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_contacts_org ON contacts(org_id);

CREATE TABLE IF NOT EXISTS lists (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT, tags TEXT,
  kind TEXT DEFAULT 'static', filter_json TEXT, created_at INTEGER,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS list_contacts (
  list_id TEXT NOT NULL, contact_id TEXT NOT NULL,
  PRIMARY KEY(list_id, contact_id),
  FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE,
  FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);

-- Global per-org do-not-contact list, enforced at queue level.
CREATE TABLE IF NOT EXISTS suppression (
  org_id TEXT NOT NULL, phone TEXT NOT NULL, reason TEXT, created_at INTEGER,
  PRIMARY KEY(org_id, phone),
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS consent_records (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, contact_phone TEXT,
  action TEXT, source TEXT, actor TEXT, created_at INTEGER,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS templates (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT, category TEXT,
  status TEXT DEFAULT 'DRAFT', meta_id TEXT, rejection_reason TEXT,
  created_at INTEGER, updated_at INTEGER,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS template_variants (
  id TEXT PRIMARY KEY, template_id TEXT NOT NULL, org_id TEXT NOT NULL,
  language TEXT, body TEXT, header_kind TEXT, header_value TEXT,
  buttons_json TEXT, example_json TEXT,
  FOREIGN KEY(template_id) REFERENCES templates(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS campaigns (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT,
  template_name TEXT, template_lang TEXT, list_id TEXT, segment_filter TEXT,
  status TEXT DEFAULT 'draft',   -- draft|scheduled|running|paused|done|cancelled
  scheduled_at INTEGER, local_time INTEGER DEFAULT 0,
  throughput INTEGER DEFAULT 20, warmup INTEGER DEFAULT 0,
  variant_a TEXT, variant_b TEXT, ab_metric TEXT,
  total INTEGER DEFAULT 0, attempted INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
  sent INTEGER DEFAULT 0, delivered INTEGER DEFAULT 0, read INTEGER DEFAULT 0,
  replied INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, clicks INTEGER DEFAULT 0,
  created_by TEXT, created_at INTEGER, finished_at INTEGER,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS campaign_recipients (
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, org_id TEXT NOT NULL,
  contact_phone TEXT, contact_name TEXT, variant TEXT DEFAULT 'a',
  idem_key TEXT UNIQUE, status TEXT DEFAULT 'queued',
  message_id TEXT, error_code INTEGER, error_label TEXT, error_fix TEXT,
  attempts INTEGER DEFAULT 0, skip_reason TEXT,
  queued_at INTEGER, sent_at INTEGER, delivered_at INTEGER, read_at INTEGER,
  FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_recip_campaign ON campaign_recipients(campaign_id);
CREATE INDEX IF NOT EXISTS idx_recip_msg ON campaign_recipients(message_id);

-- Append-only event log; campaign stats are projected from here.
CREATE TABLE IF NOT EXISTS message_events (
  id TEXT PRIMARY KEY, org_id TEXT, message_id TEXT, status TEXT,
  error_code INTEGER, payload TEXT, event_key TEXT UNIQUE, created_at INTEGER
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, contact_phone TEXT, contact_name TEXT,
  assigned_to TEXT, unread INTEGER DEFAULT 0, window_expires INTEGER,
  last_msg_at INTEGER, note TEXT,
  UNIQUE(org_id, contact_phone),
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, conversation_id TEXT,
  direction TEXT, body TEXT, message_id TEXT, status TEXT, created_at INTEGER,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS automations (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, keyword TEXT, reply TEXT,
  tag TEXT, enabled INTEGER DEFAULT 1,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY, name TEXT, price_inr INTEGER, price_usd INTEGER,
  monthly_messages INTEGER, seats INTEGER, contacts INTEGER,
  white_label INTEGER DEFAULT 0, api_access INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, plan_id TEXT, status TEXT DEFAULT 'active',
  gateway TEXT, period_start INTEGER, period_end INTEGER, created_at INTEGER,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS usage_records (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, period TEXT,
  category TEXT, country TEXT, count INTEGER DEFAULT 0, cost_micros INTEGER DEFAULT 0,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pricing (
  country TEXT, category TEXT, price_micros INTEGER,
  PRIMARY KEY(country, category)
);

CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT, prefix TEXT,
  key_hash TEXT, last_used INTEGER, revoked INTEGER DEFAULT 0, created_at INTEGER,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS outbound_webhooks (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, url TEXT, secret TEXT,
  events TEXT, enabled INTEGER DEFAULT 1,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id TEXT PRIMARY KEY, org_id TEXT, actor TEXT, action TEXT,
  target TEXT, meta TEXT, created_at INTEGER
);

CREATE TABLE IF NOT EXISTS exports (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, kind TEXT, status TEXT DEFAULT 'pending',
  path TEXT, created_at INTEGER, FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS short_links (
  id TEXT PRIMARY KEY, org_id TEXT, campaign_id TEXT, contact_phone TEXT,
  target TEXT, clicks INTEGER DEFAULT 0, created_at INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
  org_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
  PRIMARY KEY(org_id, key)
);

-- ===== v2: lifecycle automation, capture, integrations, affiliates =========
CREATE TABLE IF NOT EXISTS sequences (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT, status TEXT DEFAULT 'active',
  created_at INTEGER, FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS sequence_steps (
  id TEXT PRIMARY KEY, sequence_id TEXT NOT NULL, org_id TEXT NOT NULL, ord INTEGER,
  delay_secs INTEGER DEFAULT 0, template_name TEXT, lang TEXT DEFAULT 'en_US',
  exit_on TEXT,   -- replied|clicked|opted_out (branch/exit condition before this step)
  FOREIGN KEY(sequence_id) REFERENCES sequences(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS sequence_enrollments (
  id TEXT PRIMARY KEY, sequence_id TEXT NOT NULL, org_id TEXT NOT NULL,
  contact_phone TEXT, contact_name TEXT, step_idx INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',   -- active|completed|exited|paused
  next_run INTEGER, enrolled_at INTEGER,
  UNIQUE(sequence_id, contact_phone),
  FOREIGN KEY(sequence_id) REFERENCES sequences(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS journeys (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT, status TEXT DEFAULT 'draft',
  trigger_type TEXT, trigger_config TEXT, graph TEXT, created_at INTEGER,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS journey_runs (
  id TEXT PRIMARY KEY, journey_id TEXT NOT NULL, org_id TEXT NOT NULL,
  contact_phone TEXT, node_id TEXT, status TEXT DEFAULT 'running',
  log TEXT, next_run INTEGER, started_at INTEGER, updated_at INTEGER,
  FOREIGN KEY(journey_id) REFERENCES journeys(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS integrations (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, kind TEXT, name TEXT,
  token TEXT, config TEXT, enabled INTEGER DEFAULT 1, created_at INTEGER,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS capture_forms (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT, fields_json TEXT,
  list_id TEXT, journey_id TEXT, sequence_id TEXT, source_tag TEXT, created_at INTEGER,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ad_leads (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL, source TEXT, payload TEXT,
  contact_phone TEXT, created_at INTEGER,
  FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS affiliates (
  id TEXT PRIMARY KEY, user_id TEXT, code TEXT UNIQUE, commission_pct INTEGER DEFAULT 25,
  paid_out_micros INTEGER DEFAULT 0, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS referrals (
  id TEXT PRIMARY KEY, affiliate_id TEXT, referred_org TEXT, status TEXT DEFAULT 'signed_up',
  amount_micros INTEGER DEFAULT 0, created_at INTEGER,
  FOREIGN KEY(affiliate_id) REFERENCES affiliates(id) ON DELETE CASCADE
);
"""

# Restricted business categories (WhatsApp Commerce Policy) — onboarding screen.
RESTRICTED_CATEGORIES = {"gambling", "weapons", "adult", "tobacco", "drugs"}

DEFAULT_PLANS = [
    ("starter", "Starter", 0, 0, 1000, 2, 5000, 0, 0),
    ("growth", "Growth", 2499, 39, 25000, 5, 50000, 0, 0),
    ("enterprise", "Enterprise", 9999, 149, 500000, 50, 1000000, 1, 1),
]

# Per-message pricing (micros of INR) by country+category — admin-editable, never
# hardcoded into business logic. Meta moved to per-message pricing 2025-07-01.
DEFAULT_PRICING = [
    ("IN", "marketing", 880000), ("IN", "utility", 160000), ("IN", "authentication", 130000),
    ("US", "marketing", 2500000), ("US", "utility", 0), ("US", "authentication", 0),
    ("DEFAULT", "marketing", 1500000), ("DEFAULT", "utility", 300000), ("DEFAULT", "authentication", 250000),
]


PLAN_DESCRIPTIONS = {
    "starter": "Free trial. Try the full suite — upgrade before the trial ends to keep sending.",
    "growth": "For growing teams running regular campaigns. More messages, seats and contacts.",
    "enterprise": "White-label, public API, priority support and the highest limits.",
}

# Platform-wide settings live under this synthetic org id (no FK row needed
# because settings has no foreign key).
PLATFORM = "_platform"

# Feature-gating matrix per plan (master prompt §11). Centrally configurable;
# enforced on API + UI. Locked features show an "upgrade to unlock" prompt.
PLAN_FEATURES = {
    "starter":    {"inbox": True, "automations": True, "sequences": False, "journeys": False,
                   "chatbot": False, "integrations": False, "white_label": False, "api_access": False},
    "growth":     {"inbox": True, "automations": True, "sequences": True, "journeys": True,
                   "chatbot": False, "integrations": True, "white_label": False, "api_access": False},
    "enterprise": {"inbox": True, "automations": True, "sequences": True, "journeys": True,
                   "chatbot": True, "integrations": True, "white_label": True, "api_access": True},
}


def _add_col(c, table, col, decl):
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(conn=None):
    own = conn is None
    c = conn or raw_conn()
    c.executescript(SCHEMA)
    for p in DEFAULT_PLANS:
        c.execute("""INSERT OR IGNORE INTO plans
                     (id,name,price_inr,price_usd,monthly_messages,seats,contacts,white_label,api_access)
                     VALUES (?,?,?,?,?,?,?,?,?)""", p)
    for pr in DEFAULT_PRICING:
        c.execute("INSERT OR IGNORE INTO pricing VALUES (?,?,?)", pr)
    # ---- lightweight migrations (idempotent) ----
    _add_col(c, "plans", "description", "TEXT")
    _add_col(c, "orgs", "trial_ends", "INTEGER")
    _add_col(c, "subscriptions", "paid", "INTEGER DEFAULT 0")
    # v2 deltas
    _add_col(c, "orgs", "gst_number", "TEXT")
    _add_col(c, "orgs", "prereqs_ack", "INTEGER DEFAULT 0")
    _add_col(c, "contacts", "source", "TEXT")
    _add_col(c, "campaigns", "opt_auto_suppress", "INTEGER DEFAULT 0")
    _add_col(c, "campaigns", "opt_auto_tag", "INTEGER DEFAULT 0")
    _add_col(c, "campaigns", "opt_report_email", "TEXT")
    _add_col(c, "campaigns", "report_delay", "INTEGER DEFAULT 0")
    _add_col(c, "automations", "match_mode", "TEXT DEFAULT 'phrase'")
    _add_col(c, "plans", "features", "TEXT")   # JSON feature flags
    for pid, desc in PLAN_DESCRIPTIONS.items():
        c.execute("UPDATE plans SET description=? WHERE id=? AND (description IS NULL OR description='')",
                  (desc, pid))
    import json as _json
    for pid, feats in PLAN_FEATURES.items():
        c.execute("UPDATE plans SET features=? WHERE id=? AND (features IS NULL OR features='')",
                  (_json.dumps(feats), pid))
    # default platform trial config
    c.execute("INSERT OR IGNORE INTO settings (org_id, key, value) VALUES (?,?,?)",
              (PLATFORM, "trial", '{"trial_days": 14, "grace_days": 3}'))
    c.commit()
    if own:
        c.close()
