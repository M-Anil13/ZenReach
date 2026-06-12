"""Meta WhatsApp Cloud API client + error mapping + token health.

Graph version is pinned in config (master prompt §16). The error-code →
plain-English table is data, not control flow, so it stays admin-editable.
"""
import time

import requests

GRAPH_VERSION = "v21.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

# code -> (label, fix). Admin-editable in production (store in `settings`).
ERRORS = {
    131026: ("Number not registered on WhatsApp", "Remove or verify the number with the customer."),
    131047: ("Re-engagement window closed", "More than 24h since last reply — send an approved template."),
    131048: ("Spam rate limit hit", "Slow down; pause then resume after a cooldown. Keep quality high."),
    130429: ("Messaging tier limit reached", "Pause and resume after 24h, or raise tier by maintaining quality."),
    131056: ("Pair rate limit", "Too many messages to this number in a short window — retry later."),
    132000: ("Template parameter mismatch", "Number of {{n}} variables must match the approved template."),
    132001: ("Template not found / not approved", "Check template name + language; submit and get it approved."),
    132012: ("Template paused (low quality)", "Edit the template copy and resubmit for approval."),
    132015: ("Template disabled", "Template was disabled by Meta — create a new one."),
    190: ("Invalid / expired access token", "Generate a permanent System User token (24h temp expired)."),
    131030: ("Recipient not in allowed list", "Still on test number — add recipients or switch to verified number."),
    131031: ("Account locked / restricted", "Account restricted — open the appeal flow in API Settings."),
    100: ("Invalid parameter", "Check Phone Number ID and request fields."),
    80007: ("Rate limit (business)", "Throughput exceeded — the queue will back off and retry."),
}

# Which errors are transient (retry w/ backoff) vs permanent (fail fast).
TRANSIENT = {130429, 131048, 131056, 80007, 500, 502, 503, 504}
PERMANENT = {131026, 131030, 132000, 132001, 132012, 132015, 190}


def explain(code, fallback="Send failed"):
    label, fix = ERRORS.get(int(code or 0), (fallback, "Check the Meta error code and retry."))
    return {"code": int(code or 0), "label": label, "fix": fix}


class MetaClient:
    def __init__(self, token, phone_id=None, waba_id=None, version=GRAPH_VERSION):
        self.token = token
        self.phone_id = phone_id
        self.waba_id = waba_id
        self.base = f"https://graph.facebook.com/{version}"

    @property
    def _h(self):
        return {"Authorization": f"Bearer {self.token}"}

    def verify(self):
        """Health check against the phone number node. Returns (ok, info)."""
        try:
            r = requests.get(
                f"{self.base}/{self.phone_id}",
                params={"fields": "verified_name,display_phone_number,quality_rating,messaging_limit_tier"},
                headers=self._h, timeout=15)
            j = r.json()
            if r.status_code == 200:
                return True, j
            err = j.get("error", {})
            return False, explain(err.get("code"), err.get("message", "Verification failed"))
        except Exception as e:
            return False, {"code": 0, "label": f"Could not reach Meta API: {e}", "fix": "Check connectivity."}

    def list_templates(self):
        try:
            r = requests.get(
                f"{self.base}/{self.waba_id}/message_templates",
                params={"fields": "name,status,language,category,id,rejected_reason", "limit": 250},
                headers=self._h, timeout=20)
            return r.json().get("data", [])
        except Exception:
            return []

    def create_template(self, name, category, language, body, header=None, buttons=None, example=None):
        components = []
        if header:
            components.append({"type": "HEADER", "format": header.get("format", "TEXT"),
                               **({"text": header["text"]} if header.get("format", "TEXT") == "TEXT" else {})})
        comp_body = {"type": "BODY", "text": body}
        if example:
            comp_body["example"] = {"body_text": [example]}
        components.append(comp_body)
        if buttons:
            components.append({"type": "BUTTONS", "buttons": buttons})
        payload = {"name": name, "category": category.upper(), "language": language,
                   "components": components}
        try:
            r = requests.post(f"{self.base}/{self.waba_id}/message_templates",
                              json=payload, headers=self._h, timeout=20)
            return r.status_code == 200, r.json()
        except Exception as e:
            return False, {"error": {"message": str(e)}}

    def upload_media(self, file_bytes, mime):
        try:
            r = requests.post(
                f"{self.base}/{self.phone_id}/media",
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": ("upload", file_bytes, mime)},
                headers=self._h, timeout=60)
            return r.json().get("id")
        except Exception:
            return None

    def send_template(self, to, name, language, body_params=None, header_param=None, button_params=None):
        components = []
        if header_param:
            components.append({"type": "header", "parameters": [header_param]})
        if body_params:
            components.append({"type": "body",
                               "parameters": [{"type": "text", "text": str(p)} for p in body_params]})
        if button_params:
            for i, bp in enumerate(button_params):
                components.append({"type": "button", "sub_type": bp.get("sub_type", "url"),
                                   "index": str(i), "parameters": [bp["param"]]})
        tmpl = {"name": name, "language": {"code": language}}
        if components:
            tmpl["components"] = components
        return self._post_message({"messaging_product": "whatsapp", "to": to.lstrip("+"),
                                    "type": "template", "template": tmpl})

    def send_text(self, to, text):
        """Free-form text — only valid inside the 24h service window."""
        return self._post_message({"messaging_product": "whatsapp", "to": to.lstrip("+"),
                                    "type": "text", "text": {"body": text}})

    def _post_message(self, payload):
        try:
            r = requests.post(f"{self.base}/{self.phone_id}/messages",
                              json=payload, headers=self._h, timeout=25)
            j = r.json()
            if r.status_code == 200 and j.get("messages"):
                return True, j["messages"][0]["id"]
            err = j.get("error", {})
            return False, explain(err.get("code"), err.get("message", "Send failed"))
        except Exception as e:
            return False, explain(0, str(e))
