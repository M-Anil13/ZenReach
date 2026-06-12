"""AI assist (master prompt §13) behind a provider-agnostic interface.

Provider order (first available wins):
  1. Anthropic  — set ANTHROPIC_API_KEY (uses the `anthropic` SDK).
  2. Groq       — set GROQ_API_KEY (OpenAI-compatible, via `requests`; no SDK).
  3. Offline fallback — deterministic templated copy, always available.

Generates/rewrites campaign copy, suggests best send time, summarizes reports.
"""
import os

import requests
from flask import Blueprint, request, jsonify, g

from .db import q, q1
from .auth import require_org

bp = Blueprint("ai", __name__)
SYSTEM = "You are a WhatsApp marketing copy expert. Be concise, friendly, policy-compliant."

# Known OpenAI-compatible providers — base URL by name. Add any others by
# setting LLM_BASE_URL directly. Default model is a sensible per-provider pick;
# override with LLM_MODEL.
PROVIDERS = {
    "openai":     ("https://api.openai.com/v1", "gpt-4o-mini"),
    "groq":       ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct"),
    "together":   ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "deepseek":   ("https://api.deepseek.com/v1", "deepseek-chat"),
    "mistral":    ("https://api.mistral.ai/v1", "mistral-large-latest"),
    "xai":        ("https://api.x.ai/v1", "grok-2-latest"),
    "perplexity": ("https://api.perplexity.ai", "sonar"),
    "fireworks":  ("https://api.fireworks.ai/inference/v1", "accounts/fireworks/models/llama-v3p3-70b-instruct"),
    "ollama":     ("http://localhost:11434/v1", "llama3.1"),
}


def _llm(prompt, system=SYSTEM):
    """Provider-agnostic. Set LLM_PROVIDER + LLM_API_KEY (+ optional LLM_MODEL /
    LLM_BASE_URL) in .env. Anthropic uses its own SDK; everything else speaks the
    OpenAI chat-completions shape. Falls back to legacy ANTHROPIC/GROQ keys."""
    provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower()

    if provider == "anthropic" or (not provider and os.environ.get("ANTHROPIC_API_KEY")):
        out = _anthropic(prompt, system)
        if out is not None:
            return out

    # Resolve a generic OpenAI-compatible endpoint.
    base = os.environ.get("LLM_BASE_URL")
    model = os.environ.get("LLM_MODEL")
    key = os.environ.get("LLM_API_KEY")
    if provider in PROVIDERS:
        pb, pm = PROVIDERS[provider]
        base = base or pb
        model = model or pm
    # Legacy single-provider keys still work with no LLM_* config.
    if not key and os.environ.get("GROQ_API_KEY"):
        base = base or PROVIDERS["groq"][0]
        model = model or PROVIDERS["groq"][1]
        key = os.environ.get("GROQ_API_KEY")
    if base and model and (key or provider == "ollama"):
        return _openai_compatible(base, key, model, prompt, system)
    return None


def _anthropic(prompt, system):
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        return None
    try:
        import anthropic  # optional dependency
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(model=os.environ.get("LLM_MODEL", "claude-opus-4-8"),
                                      max_tokens=600, system=system,
                                      messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    except Exception:
        return None


def _openai_compatible(base, key, model, prompt, system):
    try:
        r = requests.post(
            base.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"} if key else {},
            json={"model": model, "max_tokens": 600,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": prompt}]},
            timeout=45)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
    except Exception:
        pass
    return None


def _fallback_copy(festival, tone, org):
    tones = {"festive": "🎉", "formal": "", "playful": "✨", "urgent": "⏰"}
    e = tones.get(tone, "🎉")
    return (f"Hi {{{{1}}}}! {e} {org or '{{2}}'} wishes you a wonderful {festival or 'celebration'}. "
            f"Enjoy an exclusive offer just for you — reply YES to claim. Valid this week only.")


@bp.post("/api/ai/generate")
@require_org
def generate():
    d = request.get_json(force=True) or {}
    festival, audience = d.get("festival", ""), d.get("audience", "customers")
    tone, language = d.get("tone", "festive"), d.get("language", "English")
    out = _llm(f"Write a WhatsApp marketing template body for a {festival} campaign aimed at "
               f"{audience}, tone: {tone}, language: {language}. Use {{{{1}}}} for the customer name "
               f"and {{{{2}}}} for the brand. One short paragraph, approval-friendly, with one emoji.")
    return jsonify({"text": out or _fallback_copy(festival, tone, g.org["name"]),
                    "provider": "llm" if out else "fallback"})


@bp.post("/api/ai/rewrite")
@require_org
def rewrite():
    d = request.get_json(force=True) or {}
    out = _llm(f"Rewrite this WhatsApp message to be {d.get('goal', 'clearer and more engaging')}, "
               f"keep {{{{1}}}}/{{{{2}}}} variables:\n\n{d.get('text', '')}")
    return jsonify({"text": out or d.get("text", ""), "provider": "llm" if out else "fallback"})


@bp.get("/api/ai/best-time")
@require_org
def best_time():
    """Best-send-time from historical read-rate by hour (data-driven, no LLM)."""
    rows = q("""SELECT strftime('%H', datetime(read_at,'unixepoch')) hr, COUNT(*) n
                FROM campaign_recipients WHERE org_id=? AND read_at IS NOT NULL GROUP BY hr
                ORDER BY n DESC LIMIT 3""", (g.org["id"],))
    if rows:
        return jsonify({"best_hours": [f"{r['hr']}:00" for r in rows], "basis": "your read-rate history"})
    return jsonify({"best_hours": ["10:00", "13:00", "19:00"], "basis": "industry default (no history yet)"})


@bp.get("/api/ai/summary/<cid>")
@require_org
def summary(cid):
    c = q1("SELECT * FROM campaigns WHERE id=? AND org_id=?", (cid, g.org["id"]))
    if not c:
        return jsonify({"error": "Not found"}), 404
    fails = q("""SELECT error_label, COUNT(*) n FROM campaign_recipients
                 WHERE campaign_id=? AND status='failed' GROUP BY error_label ORDER BY n DESC""", (cid,))
    stat = (f"Campaign '{c['name']}': {c['attempted']} attempted, {c['delivered']} delivered, "
            f"{c['read']} read, {c['failed']} failed. Top failures: "
            + (", ".join(f"{f['error_label']} ({f['n']})" for f in fails[:3]) or "none") + ".")
    out = _llm(f"Summarize this WhatsApp campaign result in 3 bullet points — what went well, what "
               f"went wrong, and the single most important next action:\n\n{stat}")
    if not out:
        rr = round(100 * c["read"] / c["delivered"], 1) if c["delivered"] else 0
        out = (f"• Delivered {c['delivered']}/{c['attempted']} with a {rr}% read rate.\n"
               f"• Main issue: {fails[0]['error_label'] if fails else 'no major failures'}.\n"
               f"• Next: {'clean invalid numbers and resend' if fails else 'scale up to a larger list'}.")
    return jsonify({"summary": out, "provider": "llm" if "•" not in out else "fallback"})
