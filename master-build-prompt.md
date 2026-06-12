# MASTER BUILD PROMPT — NexZen WhatsApp Campaign Suite (Production, v2 Final)

Copy everything below the line into any LLM / AI coding tool or hand it to a development team as the complete build specification.

---

You are a senior full-stack architect and product engineer. Build a production-grade, multi-tenant SaaS platform called **"NexZen WhatsApp Campaign Suite"** — a premium WhatsApp marketing automation product that NexZen (https://nexzen.me — a custom software studio; tagline "We Build. You Scale."; positioning "India-first. Built for the world.") will operate and sell to multiple client organizations. This is NOT a demo or prototype — every feature below must be production-quality, tested, and deployable.

## 1. Brand & UI identity
- Dark premium theme matching NexZen: page background #070A12, panel cards #0D1220 with 1px #1C2438 borders, 16px radius; accent gradient #4F7CFF → #22D3EE used for primary buttons and the logo "Zen"; text #EAF0FA, muted #8A96AD; success #34D399, warning #FBBF24, error #F87171; Inter font.
- App shell: left sidebar navigation (Dashboard, New Campaign, Contacts, Templates, Inbox, Reports, API Settings, Billing, Team), workspace/org switcher at the bottom, "Built by NexZen · We Build. You Scale." footer with link to nexzen.me on every page.
- Per-org white-labeling (Enterprise plan): custom logo, accent color, and optional custom domain.
- Fully responsive down to mobile; keyboard accessible; loading skeletons; empty states that direct the user to the next action.

## 2. Tech stack
- **Frontend:** Next.js 14+ (App Router, TypeScript), TailwindCSS with the design tokens above, TanStack Query, WebSocket client for live campaign progress, Recharts for analytics.
- **Backend:** Node.js + NestJS (TypeScript) REST API, PostgreSQL via Prisma with strict per-org row-level scoping, Redis + BullMQ for queues, S3-compatible object storage for media and exports.
- **Messaging:** official Meta WhatsApp Cloud API (graph.facebook.com/v21.0/{phone_number_id}/messages); business-initiated sends use approved message templates only; webhooks for inbound messages and status callbacks.
- **Infra:** Dockerized services, deployable to AWS (ECS/EKS) with IaC (Terraform), staging + production environments, CI/CD (GitHub Actions: lint, typecheck, tests, build, deploy), zero-downtime migrations.

## 3. Multi-tenancy & accounts
- **Organizations (workspaces):** each org owns its members, WhatsApp credentials, contacts, templates, campaigns, inbox, reports, billing plan, and audit log. Strict isolation: every DB query scoped by org_id; integration tests must prove no cross-org leakage.
- **Auth:** email+password with verification, Google OAuth, optional 2FA (TOTP); password reset; session management with refresh tokens; invitation flow to join an org by email.
- **RBAC roles:** Owner (billing + delete org), Admin (settings, credentials, members), Agent (campaigns, inbox), Viewer (read-only reports). Enforce on both API and UI.
- **Audit log:** who did what and when (credential changes, campaign launches, exports, member changes), viewable by Admins.
- **NexZen super-admin panel** (internal, separate auth realm): list all orgs with plan, usage, API-connection health, quality rating; suspend/reactivate orgs; support impersonation with consent banner and audit trail; platform revenue and usage dashboards.

## 4. WhatsApp credentials onboarding (per org)
- API Settings page with three fields: Permanent Access Token (masked, show/hide), Phone Number ID, WABA ID. "Save & verify" performs a live test Graph API call and shows a clear success badge or a mapped error (e.g., error 190 invalid/expired token → "Generate a System User token, the temporary one expires in 24h").
- Embedded illustrated step-by-step guide: create Meta Business Portfolio → create Business-type developer app → add WhatsApp product → find Phone Number ID & WABA ID on the API Setup page → create a System User (Admin) → assign app + WABA assets → generate never-expiring token with whatsapp_business_messaging + whatsapp_business_management scopes. Include prominent warning against the 24-hour temporary token, plus a short embedded video placeholder.
- **Meta Business Verification helper:** detect and display the org's verification status and messaging tier; if unverified, show a guided checklist explaining that unverified businesses are capped (250 conversations/day) and how to submit verification documents.
- Secrets stored encrypted at rest (AES-256-GCM, keys in AWS KMS/Secrets Manager), never sent to the browser after save, rotation supported, access audited.
- Restricted-industry screening at onboarding: org selects business category; categories prohibited by WhatsApp Commerce Policy (gambling, weapons, adult, certain supplements/financial products by region) are blocked with explanation.
- **Embedded Signup (strongly recommended for scale):** in addition to manual key entry, implement Meta's Embedded Signup flow so client orgs connect their WhatsApp Business Account through an in-app OAuth-style popup (no developer-portal visit needed). Prerequisites to plan for: NexZen's own Meta Business Verification, becoming a verified Tech Provider, and passing Meta App Review for Advanced Access to whatsapp_business_messaging and whatsapp_business_management (prepare screencasts and test credentials for the review). Ship manual entry first, Embedded Signup in phase 2 — it dramatically reduces onboarding drop-off.
- **Phone number sourcing & migration guidance:** built-in checklist explaining that the number must not be active on the WhatsApp/WhatsApp Business mobile app (or must be migrated, which disconnects the app and its chat history — warn loudly), OTP verification flow, and **display name approval** (Meta reviews the business display name; show its status and rejection reasons).
- **Token & connection health monitoring:** background job validates each org's token daily and before every scheduled campaign; expired/revoked tokens trigger immediate email + in-app alerts with re-connect instructions — never let a scheduled campaign silently fail on a dead token.
- **Number ban / restriction runbook:** detect account-restriction webhooks, freeze sending automatically, and surface an in-app guided appeal flow (link to Meta's appeal process, status tracking) plus a documented procedure for switching the org to a backup number without losing contact data.

## 5. Contacts module
- **Sheet upload:** .xlsx/.xls/.csv drag-and-drop, parsed server-side (streamed for large files, up to 500k rows). Auto-detect phone and name columns via header keywords (phone/mobile/whatsapp/number, name/customer/client) plus value-pattern analysis; user can override mapping via dropdowns; support extra custom columns mapped to template variables.
- **Normalization & validation:** convert every number to E.164 using libphonenumber with a selectable default country code (works for every country); validate; deduplicate within the file and against existing lists; show summary — total, valid, duplicates removed, invalid (with per-row reason); allow downloading the rejected rows as a file.
- **Lists & segmentation:** named lists with tags; merge lists; dynamic segments (filters on tags, country, language, last-engaged date); per-contact attributes (name, language, timezone derived from country code, custom fields).
- **Opt-in & consent (critical):** opt-in status + source + timestamp per contact; campaign launch requires the user to confirm the list is opted-in (checkbox + stored attestation); contacts without opt-in can be excluded by policy setting.
- **Suppression list (critical):** global per-org do-not-contact list. Inbound STOP/UNSUBSCRIBE/optout keywords (multi-language variants) automatically suppress the contact and send a one-time confirmation. Suppressed numbers are excluded from every future send even if re-uploaded in a new sheet — enforced at the queue level, not just the UI.
- **Frequency capping:** configurable rule (e.g., max 1 marketing message per contact per 24h/7d) enforced across campaigns to prevent double-messaging from overlapping lists.
- **Privacy:** per-contact data export and hard delete (GDPR + India DPDP Act); configurable data-retention periods; consent records retained for compliance.

## 6. Templates module
- **Gallery** organized by category with prebuilt, approval-friendly copy: Festival greetings (Diwali, Eid, Christmas, New Year, Holi, regional festivals), Invitations (events, webinars), Collaboration/outreach, Promotions/flash sales, Reminders (payments, appointments). Variables {name}, {org}, and custom sheet columns map to WhatsApp template parameters {{1}}, {{2}}, …
- **Template manager:** create templates in-app and submit to Meta via the WABA management API; track status (pending / approved / rejected with Meta's rejection reason / paused / disabled); category selection (Marketing / Utility / Authentication) with guidance since category affects pricing.
- **Multi-language templates:** each template supports language variants; per-contact language (from sheet column or country default) selects the right variant at send time.
- **Status sync (critical):** sync template status from Meta before every campaign launch and on webhook template-status events; block launch on paused/disabled templates with a clear fix path.
- **Media support:** header media — image (JPG/PNG ≤5MB), document (PDF ≤100MB), video (MP4 ≤16MB); uploaded to Meta's /media endpoint, media_id cached and refreshed on expiry; client-side and server-side size/type validation with friendly errors.

## 7. Campaign composer & launch engine
- **Wizard:** pick list/segment → pick approved template → fill variables (live mapping from sheet columns) → attach/confirm media → WhatsApp-style live phone preview rendering the personalized message per contact (prev/next contact navigation, media shown exactly as WhatsApp renders it) → compliance confirmation (opt-in attestation, estimated conversation cost shown) → launch now or schedule.
- **Scheduling:** send later (org timezone) and "local-time sending" — deliver at e.g. 10:00 in each contact's own timezone; pause/resume/cancel a running campaign.
- **Queue engine (BullMQ):**
  - Rate limiting per org tied to the org's current Meta messaging tier (250 → 1K → 10K → 100K → unlimited unique customers/24h), fetched and cached from Meta; configurable throughput cap (default ≤ 20 msg/sec).
  - **Number warmup mode:** for new numbers, automatic ramp-up pacing schedule with UI explanation, to protect quality rating.
  - **Retry policy:** transient errors (rate limit 130429/131048, network, 5xx) retry with exponential backoff + jitter, max attempts then dead-letter; permanent errors (recipient not on WhatsApp 131026, invalid number) fail immediately, never retried.
  - **Idempotency:** every send carries an idempotency key (campaign_id + contact_id); workers are safe to restart; exactly-once accounting in reports.
  - Tier-limit pre-check before launch: warn if the campaign exceeds remaining daily capacity and offer auto-split across days.
- **Live progress:** WebSocket-driven screen with animated progress bar and real-time counters — queued / sent / delivered / read / failed / skipped — updating as webhook callbacks arrive.

## 8. Webhook infrastructure (the heart of analytics — build early)
- Public webhook endpoint with Meta challenge verification and **X-Hub-Signature-256 validation** on every event.
- Handle: message status events (sent, delivered, read, failed with error code), inbound messages, template status changes, account quality/tier changes.
- **Idempotent processing:** Meta retries webhooks — deduplicate by message ID + status so counts are never inflated.
- Events written to an append-only message_events table, then projected into campaign stats; failed processing goes to a dead-letter queue with alerting and replay tooling.
- Quality-rating and tier-change events update the org dashboard and trigger alerts (email + in-app) when rating drops to yellow/red or a template is paused.

## 9. Inbox & auto-replies (24-hour service window)
- Shared team inbox per org: conversations from campaign replies; agent assignment, unread states, internal notes, quick-reply snippets; free-form replies allowed inside the 24-hour customer-service window, template-only outside it (UI enforces and explains the window).
- Keyword automation: auto-replies for YES/CONFIRM/JOIN etc., STOP handling wired to the suppression list, simple flow builder (keyword → reply → optional tag contact).
- Click-to-WhatsApp link and QR-code generator per org for opt-in collection.

## 10. Reports & analytics (CRM-grade, like WATI/AiSensy/DoubleTick)
- **Per-campaign report:** stat cards — total in sheet, attempted, delivered, read (read-rate %), replied, failed, skipped (invalid/suppressed/frequency-capped, each counted separately); delivery funnel chart; delivery-over-time line chart.
- **"Why it failed" breakdown:** failures grouped by Meta error code with plain-English label and suggested fix — number not on WhatsApp (131026), re-engagement window (131047), rate/tier limits (130429, 131048, 131056), template paused/quality, media upload error, recipient privacy settings, invalid parameter — rendered as horizontal bars with fix tips.
- **Per-contact log:** status timeline (queued→sent→delivered→read / failed reason / skipped reason), search and filters, export CSV/XLSX/PDF (generated async, downloadable from a Exports area, link emailed when ready).
- **Org dashboard:** campaigns over time, aggregate delivery/read/reply rates, best-performing templates, quality-rating monitor with history, opt-out rate trend, conversation-cost spend tracker.
- **A/B testing:** two template variants split across a sample, auto-rollout of the winner by read or reply rate.

## 11. Billing & metering
- Model: each org connects its **own** WABA and pays Meta directly for conversations; NexZen charges a SaaS subscription — Starter / Growth / Enterprise — with limits on monthly campaign messages, seats, contacts, and features (white-label, API access on Enterprise).
- Usage metering per org (messages launched, billable template messages by category Marketing/Utility/Authentication, seats); plan enforcement with soft warnings then hard caps; proration on upgrades.
- Payments via Razorpay (India) + Stripe (international); invoices with GST support; dunning for failed payments; cancel/downgrade flows.
- Cost transparency: show estimated Meta cost per campaign. IMPORTANT: Meta switched from conversation-based to **per-message pricing for template messages (effective July 1, 2025)** — price by template category (Marketing / Utility / Authentication) and recipient country; utility templates sent inside an open 24h service window are free. Maintain the per-country rate table as updatable config (admin-panel editable), never hardcoded, with a "rates last updated" stamp.
- Also meter "usage" by template category since Marketing messages cost more than Utility — encourage orgs to classify reminders as Utility to save money (built-in tip).

## 12. Public API & integrations (Enterprise)
- REST API with org-scoped API keys: manage contacts, trigger campaigns, fetch reports; outbound webhooks to the client's systems (campaign.completed, message.failed, contact.opted_out); rate-limited and documented (OpenAPI + docs site).
- CSV/Sheet importers plus optional native integrations roadmap (Google Sheets sync, Shopify customer import, generic CRM webhook).

## 13. AI assist (premium differentiator)
- Generate or rewrite campaign copy by festival/audience/tone/language; suggest template variable usage; best-send-time suggestion per region from historical read-rate data; AI summaries of campaign reports ("what went wrong and what to do next"). Implement behind a provider-agnostic interface.

## 14. Security, compliance & legal
- OWASP hygiene: input validation everywhere, rate limiting, CSRF, secure headers, dependency scanning; secrets in KMS; TLS everywhere; encrypted backups.
- DPDP (India) + GDPR: consent records, data export, right-to-erasure, retention policies, DPA template per org; privacy policy and terms of service pages wired into signup.
- Compliance guardrails in UX: opt-in attestation before launch, suppression enforcement at queue level, restricted-category blocking, tier and quality warnings.
- Pen-test-ready: document threat model; admin actions always audited.

## 15. Operations & quality
- Observability: structured JSON logs, OpenTelemetry traces, Prometheus/Grafana metrics (queue depth, send rate, webhook lag, failure-rate by error code) with alerting; Sentry for errors; uptime monitoring and status page.
- Backups: automated Postgres backups with tested restore; media lifecycle policies.
- **Demo/sandbox mode per org:** when credentials aren't connected, the full flow runs with simulated sending and clearly labeled fake data — used for sales demos and onboarding.
- Testing: unit tests (parser, phone normalization across countries, suppression, frequency cap, retry classification), integration tests (multi-tenant isolation, webhook idempotency, queue restart safety), E2E happy path (upload → launch → report) in CI; seed scripts.
- Docs: in-app help center containing the credentials setup guide, troubleshooting (error 190, 131030, 131047), number-warmup explainer, and template approval tips.

## 16. Final production hardening & growth features
- **Rich template types:** support interactive buttons — quick-reply buttons, URL buttons, and copy-code coupon buttons; carousel templates (multi-card product showcases); limited-time-offer templates. These are what make campaigns convert.
- **Click tracking:** URL buttons route through org-branded short links with per-contact click attribution; clicks appear in campaign reports (click-through rate alongside read rate) and as a contact-engagement signal for segments.
- **WhatsApp Flows:** native in-chat forms (lead capture, RSVP, appointment booking, feedback surveys) built with a visual flow designer; responses land in contact attributes and exports. Major premium differentiator — plan for phase 2.
- **Commerce roadmap:** product catalogs, single/multi-product messages, and WhatsApp Payments (UPI in India) — design the schema so these bolt on later without migration pain.
- **Fallback channel:** optional SMS/email fallback for permanently failed WhatsApp sends (number not on WhatsApp) via pluggable providers — turns failures into recovered reach and is a strong sales point.
- **Graph API version policy:** pin the Meta Graph API version in config; Meta sunsets versions roughly every two years — schedule quarterly review, integration tests run against the pinned version, and a documented upgrade procedure. Same for the error-code → plain-English mapping table: keep it admin-editable, not hardcoded.
- **App UI localization:** i18n framework from day one (English first; Hindi, Telugu, Arabic, Spanish next) since client orgs are global.
- **Data residency & DR:** primary region ap-south-1 (Mumbai) with documented options for other regions; disaster recovery with defined RTO ≤ 4h / RPO ≤ 15min, tested restore drills; load testing for 1M-contact uploads and 100k-message campaigns before launch.
- **Customer success layer:** in-app support widget + ticketing, public status page, SLA terms per plan (e.g., 99.9% on Enterprise), onboarding checklist with downloadable sample contact sheet, interactive product tour, and "campaign health" pre-flight checks (template approved? token valid? within tier? list opted-in?) shown as a green checklist before every launch.
- **Legal note baked into onboarding:** consent/anti-spam law varies by country (e.g., TCPA-style rules, EU ePrivacy); the platform enforces Meta policy and opt-in records, but advise orgs to confirm local-law compliance — display this disclaimer at campaign launch and in the DPA.

## 17. Deliverables
1. Complete PostgreSQL schema (orgs, users, memberships, credentials, contacts, lists, segments, suppression, consent_records, templates, template_variants, campaigns, campaign_recipients, message_events, conversations, messages, plans, subscriptions, usage_records, api_keys, audit_logs, exports).
2. NestJS API with all routes, queue workers (sender, scheduler, exporter, webhook processor), and the webhook handler.
3. Next.js frontend implementing every screen above in the NexZen identity.
4. Terraform + Docker deployment, CI/CD pipelines, staging environment.
5. Test suites and seed/demo data.
6. Admin panel for NexZen.

Build it in this order: (1) auth + multi-tenancy + manual credentials onboarding, (2) contacts + suppression/opt-in, (3) templates (incl. buttons) + Meta sync, (4) queue engine + webhook infrastructure with idempotency + token health monitoring, (5) campaign wizard + pre-flight checks + live progress, (6) reports + click tracking, (7) billing/metering (per-message pricing), (8) inbox + automation, (9) Embedded Signup + WhatsApp Flows + AI assist + A/B + white-label + public API + fallback channel, (10) admin panel + observability + DR drills + load testing.

---

End of prompt.
