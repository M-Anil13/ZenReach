import { useState, useMemo, useRef, useEffect } from "react";
import * as XLSX from "xlsx";

/* ================= NexZen brand tokens ================= */
const T = {
  bg: "#070A12",
  panel: "#0D1220",
  panelSoft: "#111729",
  line: "#1C2438",
  text: "#EAF0FA",
  dim: "#8A96AD",
  accent: "#4F7CFF",
  accent2: "#22D3EE",
  green: "#34D399",
  amber: "#FBBF24",
  red: "#F87171",
  grad: "linear-gradient(92deg,#4F7CFF,#22D3EE)",
};

/* ================= helpers ================= */
const cleanPhone = (raw) => String(raw ?? "").replace(/[^\d+]/g, "");
const isValidPhone = (raw) => {
  const d = cleanPhone(raw).replace(/\D/g, "");
  return d.length >= 8 && d.length <= 15;
};
const toE164 = (raw, cc) => {
  let p = cleanPhone(raw);
  if (p.startsWith("+")) return p;
  if (p.startsWith("00")) return "+" + p.slice(2);
  const d = p.replace(/\D/g, "");
  return "+" + (d.length <= 10 && cc ? cc + d : d);
};
const guessCol = (headers, rows, kind) => {
  const keys =
    kind === "phone"
      ? ["phone", "mobile", "number", "whatsapp", "contact", "msisdn"]
      : ["name", "first", "customer", "client", "person"];
  const byHdr = headers.findIndex((h) =>
    keys.some((k) => String(h).toLowerCase().includes(k))
  );
  if (byHdr !== -1) return byHdr;
  if (kind === "phone")
    return headers.findIndex((_, i) =>
      rows.slice(0, 8).filter((r) => isValidPhone(r[i])).length >= 1
    );
  return headers.findIndex((_, i) =>
    rows.slice(0, 8).filter((r) => /^[A-Za-z .'-]{2,}$/.test(String(r[i] ?? ""))).length >= 1
  );
};

const TEMPLATES = [
  { cat: "Festival", label: "Diwali greeting", emoji: "🪔", text: "Hi {name}! 🪔 Team {org} wishes you a sparkling Diwali. Celebrate with a festive 20% off — valid till Sunday. Reply YES to claim." },
  { cat: "Festival", label: "Eid greeting", emoji: "🌙", text: "Eid Mubarak, {name}! 🌙 May this Eid bring joy to you and your family. As a token of celebration, enjoy free delivery on your next order from {org}." },
  { cat: "Festival", label: "New Year", emoji: "🎆", text: "Happy New Year, {name}! 🎆 Thank you for being with {org} this year. Here's to bigger things in the next one — your loyalty reward is waiting inside." },
  { cat: "Invitation", label: "Event invite", emoji: "🎟️", text: "Hello {name}, you're invited to {org}'s product launch on 28 June, 6 PM at Hitex, Hyderabad. Seats are limited — reply CONFIRM to reserve yours." },
  { cat: "Invitation", label: "Webinar invite", emoji: "💻", text: "Hi {name}, join our free live webinar this Saturday at 11 AM — \"Growing your business on WhatsApp\". Reply JOIN and we'll send the link." },
  { cat: "Collaboration", label: "Partner outreach", emoji: "🤝", text: "Hi {name}, we loved your recent work! {org} is planning a collaboration for our upcoming campaign and we'd like to share a short brief. Open to a quick chat this week?" },
  { cat: "Promotion", label: "Flash sale", emoji: "⚡", text: "{name}, our 48-hour flash sale is LIVE ⚡ Up to 40% off across the store. Tap the catalog below before it ends." },
  { cat: "Reminder", label: "Payment reminder", emoji: "🧾", text: "Hi {name}, a gentle reminder that your invoice from {org} is due this Friday. The PDF is attached — reply PAID once done, or HELP for assistance." },
];

const SAMPLE = [
  ["Customer Name", "WhatsApp Number", "City"],
  ["Ananya Sharma", "+91 98480 12345", "Hyderabad"],
  ["Ravi Teja", "9848123456", "Warangal"],
  ["Fatima Khan", "+971 50 123 4567", "Dubai"],
  ["John Carter", "+1 (415) 555-0132", "San Francisco"],
  ["Mei Lin", "+65 8123 4567", "Singapore"],
  ["Carlos Ruiz", "+52 55 1234 5678", "Mexico City"],
  ["Priya Verma", "98491-22334", "Hyderabad"],
  ["Aisha Bello", "+234 803 123 4567", "Lagos"],
  ["Tom Müller", "+49 151 2345 6789", "Berlin"],
  ["Sneha Rao", "9000011122", "Bengaluru"],
  ["Bad Row", "12ab", "—"],
];

const FAIL_REASONS = [
  { code: "NOT_ON_WA", label: "Number not registered on WhatsApp", fix: "Remove or verify the number with the customer.", w: 0.45 },
  { code: "RATE_LIMIT", label: "Messaging tier limit reached", fix: "Pause and resume after 24h, or raise your tier by maintaining quality rating.", w: 0.2 },
  { code: "TEMPLATE_PAUSED", label: "Template paused by Meta (low quality)", fix: "Edit the template copy and resubmit for approval.", w: 0.15 },
  { code: "PRIVACY", label: "Recipient privacy settings block business messages", fix: "Reach the customer via another channel for opt-in.", w: 0.1 },
  { code: "MEDIA_ERR", label: "Attached media failed to upload", fix: "Keep images < 5 MB (JPG/PNG) and documents < 100 MB (PDF).", w: 0.1 },
];
const pickReason = () => {
  let r = Math.random(), acc = 0;
  for (const f of FAIL_REASONS) { acc += f.w; if (r <= acc) return f; }
  return FAIL_REASONS[0];
};

/* ================= tiny UI atoms ================= */
const Card = ({ children, style }) => (
  <div style={{ background: T.panel, border: `1px solid ${T.line}`, borderRadius: 16, padding: 22, ...style }}>{children}</div>
);
const Btn = ({ primary, danger, children, style, ...p }) => (
  <button
    {...p}
    style={{
      padding: "10px 18px", borderRadius: 10, fontWeight: 700, fontSize: 13.5, cursor: "pointer",
      border: primary || danger ? "none" : `1px solid ${T.line}`,
      background: primary ? T.grad : danger ? T.red : "transparent",
      color: primary || danger ? "#06101e" : T.text, ...style,
    }}
  >{children}</button>
);
const Field = ({ label, children, hint }) => (
  <label style={{ display: "block", fontSize: 13, marginBottom: 14 }}>
    <div style={{ color: T.dim, marginBottom: 5, fontWeight: 600 }}>{label}</div>
    {children}
    {hint && <div style={{ color: T.dim, fontSize: 11.5, marginTop: 4 }}>{hint}</div>}
  </label>
);
const inputStyle = {
  width: "100%", boxSizing: "border-box", background: T.bg, color: T.text,
  border: `1px solid ${T.line}`, borderRadius: 9, padding: "10px 12px", fontSize: 13.5,
};
const Badge = ({ tone, children }) => {
  const c = { ok: [T.green, "#0d2a1f"], warn: [T.amber, "#2b2310"], bad: [T.red, "#2b1414"], info: [T.accent2, "#0c2230"] }[tone];
  return <span style={{ background: c[1], color: c[0], padding: "2px 9px", borderRadius: 999, fontSize: 11, fontWeight: 700 }}>{children}</span>;
};
const Stat = ({ value, label, color }) => (
  <div>
    <div style={{ fontSize: 28, fontWeight: 800, color: color || T.text, lineHeight: 1.1 }}>{value}</div>
    <div style={{ fontSize: 12, color: T.dim }}>{label}</div>
  </div>
);

/* ================= WhatsApp preview ================= */
function PhonePreview({ name, text, org, media }) {
  const rendered = (text || "Your message preview…")
    .replaceAll("{name}", name || "there")
    .replaceAll("{org}", org || "your brand");
  return (
    <div style={{ width: 286, borderRadius: 22, border: `1px solid ${T.line}`, overflow: "hidden", background: "#0B141A", flexShrink: 0 }}>
      <div style={{ background: "#1a242c", padding: "10px 14px", display: "flex", gap: 10, alignItems: "center" }}>
        <div style={{ width: 30, height: 30, borderRadius: "50%", background: T.grad, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800, color: "#06101e", fontSize: 13 }}>
          {(name || "C")[0].toUpperCase()}
        </div>
        <div>
          <div style={{ fontSize: 13, fontWeight: 700, color: "#e9edef" }}>{name || "Contact"}</div>
          <div style={{ fontSize: 11, color: "#8696a0" }}>online</div>
        </div>
      </div>
      <div style={{ padding: 14, minHeight: 190 }}>
        <div style={{ background: "#005C4B", color: "#e9edef", borderRadius: "12px 0 12px 12px", padding: 10, fontSize: 13, lineHeight: 1.45, marginLeft: 22, whiteSpace: "pre-wrap" }}>
          {media && media.kind === "image" && (
            <img src={media.url} alt="" style={{ width: "100%", borderRadius: 8, marginBottom: 8, display: "block" }} />
          )}
          {media && media.kind === "doc" && (
            <div style={{ background: "#0b3d33", borderRadius: 8, padding: "8px 10px", marginBottom: 8, display: "flex", gap: 8, alignItems: "center", fontSize: 12 }}>
              <span style={{ fontSize: 18 }}>📄</span>
              <div>
                <div style={{ fontWeight: 700 }}>{media.name}</div>
                <div style={{ color: "#9fd3c7", fontSize: 10.5 }}>{media.size} · PDF</div>
              </div>
            </div>
          )}
          {rendered}
          <div style={{ fontSize: 10, color: "#9fd3c7", textAlign: "right", marginTop: 6 }}>12:05 ✓✓</div>
        </div>
      </div>
    </div>
  );
}

/* ================= main app ================= */
export default function App() {
  const [view, setView] = useState("campaign"); // campaign | templates | reports | settings
  const [step, setStep] = useState(0);
  const [org, setOrg] = useState("BrightMart Retail");
  const [headers, setHeaders] = useState([]);
  const [rows, setRows] = useState([]);
  const [fileName, setFileName] = useState("");
  const [phoneCol, setPhoneCol] = useState(-1);
  const [nameCol, setNameCol] = useState(-1);
  const [cc, setCc] = useState("91");
  const [message, setMessage] = useState(TEMPLATES[0].text);
  const [media, setMedia] = useState(null);
  const [previewIdx, setPreviewIdx] = useState(0);
  const [sending, setSending] = useState(false);
  const [results, setResults] = useState([]);
  const [report, setReport] = useState(null);
  const [creds, setCreds] = useState({ token: "", phoneId: "", wabaId: "" });
  const [showToken, setShowToken] = useState(false);
  const fileRef = useRef(null);
  const mediaRef = useRef(null);
  const taRef = useRef(null);
  const timerRef = useRef(null);
  useEffect(() => () => clearInterval(timerRef.current), []);

  const credsOk = creds.token && creds.phoneId;

  const loadMatrix = (m, name) => {
    const [hdr, ...data] = m.filter((r) => r.some((c) => c !== "" && c != null));
    setHeaders(hdr.map(String)); setRows(data); setFileName(name);
    setPhoneCol(guessCol(hdr, data, "phone")); setNameCol(guessCol(hdr, data, "name"));
    setStep(1);
  };
  const handleFile = async (f) => {
    const buf = await f.arrayBuffer();
    const wb = XLSX.read(buf, { type: "array" });
    loadMatrix(XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]], { header: 1, defval: "" }), f.name);
  };
  const handleMedia = (f) => {
    if (!f) return;
    const isImg = f.type.startsWith("image/");
    setMedia({
      kind: isImg ? "image" : "doc",
      name: f.name,
      size: (f.size / 1024).toFixed(0) + " KB",
      url: isImg ? URL.createObjectURL(f) : null,
    });
  };

  const contacts = useMemo(() => {
    if (phoneCol < 0) return [];
    return rows.map((r, i) => {
      const valid = isValidPhone(r[phoneCol]);
      return {
        idx: i,
        name: nameCol >= 0 ? String(r[nameCol] ?? "").trim() : "",
        phone: valid ? toE164(r[phoneCol], cc) : String(r[phoneCol] ?? ""),
        valid,
      };
    });
  }, [rows, phoneCol, nameCol, cc]);
  const valid = contacts.filter((c) => c.valid);
  const skipped = contacts.filter((c) => !c.valid);

  const startCampaign = () => {
    clearInterval(timerRef.current);
    setSending(true); setStep(3); setResults([]); setReport(null);
    let i = 0; const out = [];
    timerRef.current = setInterval(() => {
      if (i >= valid.length) {
        clearInterval(timerRef.current); setSending(false);
        const delivered = out.filter((r) => r.status === "delivered");
        const read = delivered.filter(() => Math.random() < 0.62).length;
        const failed = out.filter((r) => r.status === "failed");
        const reasons = {};
        failed.forEach((f) => { reasons[f.reason.label] = (reasons[f.reason.label] || 0) + 1; });
        setReport({
          id: "CMP-" + Date.now().toString().slice(-6),
          when: new Date().toLocaleString(),
          total: contacts.length, attempted: valid.length, skipped: skipped.length,
          delivered: delivered.length, read, failed: failed.length,
          reasons, rows: out, skippedRows: skipped,
        });
        setView("reports");
        return;
      }
      const c = valid[i];
      const fail = Math.random() < 0.12;
      out.push({ ...c, status: fail ? "failed" : "delivered", reason: fail ? pickReason() : null });
      setResults([...out]); i++;
    }, 140);
  };

  const exportCSV = () => {
    if (!report) return;
    const lines = [["Name", "Phone", "Status", "Failure reason"].join(",")];
    report.rows.forEach((r) => lines.push([r.name, r.phone, r.status, r.reason?.label || ""].map((x) => `"${x}"`).join(",")));
    report.skippedRows.forEach((r) => lines.push([r.name, r.phone, "skipped", "Invalid number in sheet"].map((x) => `"${x}"`).join(",")));
    const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = report.id + "-report.csv"; a.click();
    URL.revokeObjectURL(url);
  };

  const done = results.length;
  const pct = valid.length ? Math.round((done / valid.length) * 100) : 0;
  const pc = valid[previewIdx] || valid[0];

  const NavBtn = ({ id, label, icon }) => (
    <button onClick={() => setView(id)} style={{
      display: "flex", gap: 10, alignItems: "center", width: "100%", textAlign: "left",
      padding: "11px 14px", borderRadius: 10, border: "none", cursor: "pointer", fontSize: 13.5, fontWeight: 600,
      background: view === id ? "rgba(79,124,255,.14)" : "transparent",
      color: view === id ? T.text : T.dim,
      borderLeft: view === id ? `3px solid ${T.accent}` : "3px solid transparent",
    }}>
      <span>{icon}</span>{label}
    </button>
  );

  return (
    <div style={{ minHeight: "100vh", background: T.bg, color: T.text, fontFamily: "'Inter','Segoe UI',system-ui,sans-serif", display: "flex" }}>
      {/* ===== sidebar ===== */}
      <aside style={{ width: 230, borderRight: `1px solid ${T.line}`, padding: "22px 14px", display: "flex", flexDirection: "column", gap: 4, flexShrink: 0 }}>
        <div style={{ padding: "0 10px 18px" }}>
          <div style={{ fontSize: 20, fontWeight: 800, letterSpacing: "-0.02em" }}>
            Nex<span style={{ background: T.grad, WebkitBackgroundClip: "text", color: "transparent" }}>Zen</span>
          </div>
          <div style={{ fontSize: 11, color: T.dim, marginTop: 2 }}>WhatsApp Campaign Suite</div>
        </div>
        <NavBtn id="campaign" label="New campaign" icon="🚀" />
        <NavBtn id="templates" label="Templates" icon="🗂️" />
        <NavBtn id="reports" label="Reports" icon="📊" />
        <NavBtn id="settings" label="API settings" icon="🔑" />
        <div style={{ marginTop: "auto", padding: "14px 10px", fontSize: 11, color: T.dim, lineHeight: 1.5 }}>
          <div style={{ marginBottom: 6 }}>
            Workspace
            <select value={org} onChange={(e) => setOrg(e.target.value)} style={{ ...inputStyle, marginTop: 4, padding: "7px 8px", fontSize: 12 }}>
              <option>BrightMart Retail</option>
              <option>Lotus Events Co.</option>
              <option>Zora Fashion</option>
            </select>
          </div>
          Built by <a href="https://nexzen.me" target="_blank" rel="noreferrer" style={{ color: T.accent2, textDecoration: "none", fontWeight: 700 }}>NexZen</a>
          <div>India-first · Built for the world</div>
        </div>
      </aside>

      {/* ===== main ===== */}
      <main style={{ flex: 1, padding: "26px 30px", maxWidth: 1000 }}>
        {!credsOk && view !== "settings" && (
          <div style={{ background: "#2b2310", border: `1px solid #4d3f12`, color: T.amber, borderRadius: 12, padding: "12px 16px", fontSize: 13, marginBottom: 18, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <span>⚠️ WhatsApp API is not connected yet — campaigns will run in demo mode.</span>
            <Btn onClick={() => setView("settings")} style={{ padding: "7px 14px" }}>Connect now →</Btn>
          </div>
        )}

        {/* ---------- CAMPAIGN WIZARD ---------- */}
        {view === "campaign" && (
          <>
            <h1 style={{ fontSize: 22, fontWeight: 800, margin: "0 0 4px" }}>New campaign</h1>
            <p style={{ color: T.dim, fontSize: 13.5, margin: "0 0 20px" }}>Upload contacts → review → compose with media → launch.</p>
            <div style={{ display: "flex", gap: 8, marginBottom: 20, flexWrap: "wrap" }}>
              {["1 · Upload", "2 · Review", "3 · Compose", "4 · Launch"].map((s, i) => (
                <div key={s} style={{
                  padding: "6px 14px", borderRadius: 999, fontSize: 12.5, fontWeight: 700,
                  background: step === i ? "rgba(79,124,255,.16)" : step > i ? "rgba(52,211,153,.1)" : T.panel,
                  color: step === i ? T.accent : step > i ? T.green : T.dim,
                  border: `1px solid ${step === i ? T.accent : T.line}`,
                }}>{step > i ? "✓ " : ""}{s}</div>
              ))}
            </div>

            {step === 0 && (
              <Card>
                <div
                  onClick={() => fileRef.current?.click()}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => { e.preventDefault(); const f = e.dataTransfer.files?.[0]; if (f) handleFile(f); }}
                  style={{ border: `2px dashed ${T.line}`, borderRadius: 14, padding: "46px 20px", textAlign: "center", cursor: "pointer", marginBottom: 16 }}
                >
                  <div style={{ fontSize: 34 }}>📄</div>
                  <div style={{ fontWeight: 700, marginTop: 6 }}>Drop your contact sheet, or click to browse</div>
                  <div style={{ color: T.dim, fontSize: 13, marginTop: 4 }}>.xlsx · .xls · .csv — we auto-detect name & phone columns</div>
                  <input ref={fileRef} type="file" accept=".xlsx,.xls,.csv" style={{ display: "none" }}
                    onChange={(e) => e.target.files?.[0] && handleFile(e.target.files[0])} />
                </div>
                <Btn onClick={() => loadMatrix(SAMPLE, "sample_contacts.xlsx")}>Try with sample data</Btn>
              </Card>
            )}

            {step === 1 && (
              <Card>
                <div style={{ fontWeight: 700, marginBottom: 4 }}>Review detected columns</div>
                <div style={{ color: T.dim, fontSize: 13, marginBottom: 16 }}>{fileName} · {rows.length} rows</div>
                <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
                  <Field label="Phone column">
                    <select style={inputStyle} value={phoneCol} onChange={(e) => setPhoneCol(+e.target.value)}>
                      <option value={-1}>— select —</option>
                      {headers.map((h, i) => <option key={i} value={i}>{h}</option>)}
                    </select>
                  </Field>
                  <Field label="Name column">
                    <select style={inputStyle} value={nameCol} onChange={(e) => setNameCol(+e.target.value)}>
                      <option value={-1}>— select —</option>
                      {headers.map((h, i) => <option key={i} value={i}>{h}</option>)}
                    </select>
                  </Field>
                  <Field label="Default country code" hint="Applied to numbers missing one — makes it work globally.">
                    <select style={inputStyle} value={cc} onChange={(e) => setCc(e.target.value)}>
                      <option value="91">+91 India</option><option value="1">+1 US/Canada</option>
                      <option value="44">+44 UK</option><option value="971">+971 UAE</option>
                      <option value="65">+65 Singapore</option><option value="234">+234 Nigeria</option>
                      <option value="49">+49 Germany</option><option value="52">+52 Mexico</option>
                    </select>
                  </Field>
                </div>
                <div style={{ overflowX: "auto", margin: "6px 0 16px" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                    <thead><tr>{["#", "Name", "Phone (E.164)", "Status"].map((h) => (
                      <th key={h} style={{ textAlign: "left", padding: "8px 10px", color: T.dim, borderBottom: `1px solid ${T.line}`, fontWeight: 600 }}>{h}</th>))}</tr></thead>
                    <tbody>
                      {contacts.slice(0, 6).map((c) => (
                        <tr key={c.idx}>
                          <td style={{ padding: "8px 10px", borderBottom: `1px solid ${T.line}` }}>{c.idx + 1}</td>
                          <td style={{ padding: "8px 10px", borderBottom: `1px solid ${T.line}` }}>{c.name || "—"}</td>
                          <td style={{ padding: "8px 10px", borderBottom: `1px solid ${T.line}` }}>{c.phone}</td>
                          <td style={{ padding: "8px 10px", borderBottom: `1px solid ${T.line}` }}>
                            <Badge tone={c.valid ? "ok" : "bad"}>{c.valid ? "Valid" : "Invalid"}</Badge>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {contacts.length > 6 && <div style={{ color: T.dim, fontSize: 12, padding: "8px 10px" }}>+ {contacts.length - 6} more rows</div>}
                </div>
                <div style={{ display: "flex", gap: 24, alignItems: "center", flexWrap: "wrap" }}>
                  <Stat value={valid.length} label="valid contacts" color={T.green} />
                  <Stat value={skipped.length} label="will be skipped" color={T.red} />
                  <div style={{ marginLeft: "auto", display: "flex", gap: 10 }}>
                    <Btn onClick={() => setStep(0)}>Back</Btn>
                    <Btn primary disabled={!valid.length} onClick={() => setStep(2)} style={{ opacity: valid.length ? 1 : 0.5 }}>Continue → Compose</Btn>
                  </div>
                </div>
              </Card>
            )}

            {step === 2 && (
              <Card>
                <div style={{ fontWeight: 700, marginBottom: 12 }}>Compose message</div>
                <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
                  <div style={{ flex: "1 1 400px", minWidth: 300 }}>
                    <div style={{ display: "flex", gap: 6, marginBottom: 10, flexWrap: "wrap" }}>
                      {TEMPLATES.slice(0, 5).map((t) => (
                        <Btn key={t.label} style={{ padding: "5px 11px", fontSize: 12 }} onClick={() => setMessage(t.text)}>{t.emoji} {t.label}</Btn>
                      ))}
                      <Btn style={{ padding: "5px 11px", fontSize: 12, color: T.accent2 }} onClick={() => setView("templates")}>All templates →</Btn>
                    </div>
                    <textarea ref={taRef} value={message} onChange={(e) => setMessage(e.target.value)} rows={7}
                      style={{ ...inputStyle, lineHeight: 1.5, resize: "vertical" }} />
                    <div style={{ display: "flex", gap: 8, marginTop: 10, alignItems: "center", flexWrap: "wrap" }}>
                      <Btn style={{ fontSize: 12, padding: "6px 12px" }} onClick={() => {
                        const ta = taRef.current; if (!ta) return setMessage((m) => m + " {name}");
                        const { selectionStart: s, selectionEnd: e } = ta;
                        setMessage((m) => m.slice(0, s) + "{name}" + m.slice(e));
                      }}>+ {"{name}"}</Btn>
                      <Btn style={{ fontSize: 12, padding: "6px 12px" }} onClick={() => setMessage((m) => m + " {org}")}>+ {"{org}"}</Btn>
                      <Btn style={{ fontSize: 12, padding: "6px 12px" }} onClick={() => mediaRef.current?.click()}>📎 Attach image / PDF</Btn>
                      <input ref={mediaRef} type="file" accept="image/*,.pdf" style={{ display: "none" }}
                        onChange={(e) => handleMedia(e.target.files?.[0])} />
                      {media && (
                        <span style={{ fontSize: 12, color: T.accent2 }}>
                          {media.kind === "image" ? "🖼️" : "📄"} {media.name}
                          <button onClick={() => setMedia(null)} style={{ background: "none", border: "none", color: T.red, cursor: "pointer", fontWeight: 800, marginLeft: 6 }}>✕</button>
                        </span>
                      )}
                    </div>
                    <div style={{ display: "flex", gap: 10, marginTop: 16, justifyContent: "flex-end" }}>
                      <Btn onClick={() => setStep(1)}>Back</Btn>
                      <Btn primary onClick={startCampaign}>Launch campaign ({valid.length})</Btn>
                    </div>
                  </div>
                  <div>
                    <PhonePreview name={pc?.name} text={message} org={org} media={media} />
                    <div style={{ display: "flex", gap: 8, marginTop: 10, justifyContent: "center", fontSize: 12 }}>
                      <Btn style={{ padding: "4px 12px", fontSize: 12 }} onClick={() => setPreviewIdx((i) => Math.max(0, i - 1))}>‹</Btn>
                      <span style={{ color: T.dim, alignSelf: "center" }}>{Math.min(previewIdx + 1, valid.length)} / {valid.length}</span>
                      <Btn style={{ padding: "4px 12px", fontSize: 12 }} onClick={() => setPreviewIdx((i) => Math.min(valid.length - 1, i + 1))}>›</Btn>
                    </div>
                  </div>
                </div>
              </Card>
            )}

            {step === 3 && (
              <Card>
                <div style={{ fontWeight: 700, marginBottom: 4 }}>{sending ? "Campaign running…" : "Finished — opening report"}</div>
                <div style={{ color: T.dim, fontSize: 13, marginBottom: 14 }}>{done} of {valid.length} processed {!credsOk && "· demo mode"}</div>
                <div style={{ height: 14, background: T.bg, border: `1px solid ${T.line}`, borderRadius: 999, overflow: "hidden", marginBottom: 16 }}>
                  <div style={{ width: pct + "%", height: "100%", background: T.grad, transition: "width .15s" }} />
                </div>
                <div style={{ display: "flex", gap: 26, flexWrap: "wrap" }}>
                  <Stat value={results.filter((r) => r.status === "delivered").length} label="delivered" color={T.green} />
                  <Stat value={results.filter((r) => r.status === "failed").length} label="failed" color={T.red} />
                  <Stat value={pct + "%"} label="progress" />
                </div>
              </Card>
            )}
          </>
        )}

        {/* ---------- TEMPLATES ---------- */}
        {view === "templates" && (
          <>
            <h1 style={{ fontSize: 22, fontWeight: 800, margin: "0 0 4px" }}>Message templates</h1>
            <p style={{ color: T.dim, fontSize: 13.5, margin: "0 0 20px" }}>
              Ready-made, Meta-approval-friendly templates. Variables {"{name}"} and {"{org}"} are filled automatically.
            </p>
            {["Festival", "Invitation", "Collaboration", "Promotion", "Reminder"].map((cat) => (
              <div key={cat} style={{ marginBottom: 22 }}>
                <div style={{ fontSize: 12, fontWeight: 800, color: T.accent2, letterSpacing: ".08em", textTransform: "uppercase", marginBottom: 10 }}>{cat}</div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(280px,1fr))", gap: 12 }}>
                  {TEMPLATES.filter((t) => t.cat === cat).map((t) => (
                    <Card key={t.label} style={{ padding: 16 }}>
                      <div style={{ fontWeight: 700, marginBottom: 6 }}>{t.emoji} {t.label}</div>
                      <div style={{ fontSize: 12.5, color: T.dim, lineHeight: 1.5, marginBottom: 12 }}>{t.text}</div>
                      <Btn primary style={{ padding: "7px 14px", fontSize: 12 }} onClick={() => { setMessage(t.text); setView("campaign"); if (rows.length) setStep(2); }}>
                        Use this template
                      </Btn>
                    </Card>
                  ))}
                </div>
              </div>
            ))}
          </>
        )}

        {/* ---------- REPORTS ---------- */}
        {view === "reports" && (
          <>
            <h1 style={{ fontSize: 22, fontWeight: 800, margin: "0 0 4px" }}>Campaign report</h1>
            {!report ? (
              <Card style={{ marginTop: 16, textAlign: "center", padding: 50 }}>
                <div style={{ fontSize: 34 }}>📊</div>
                <div style={{ fontWeight: 700, marginTop: 8 }}>No campaigns yet</div>
                <div style={{ color: T.dim, fontSize: 13, margin: "6px 0 16px" }}>Run your first campaign to see delivery analytics here.</div>
                <Btn primary onClick={() => setView("campaign")}>Start a campaign</Btn>
              </Card>
            ) : (
              <>
                <p style={{ color: T.dim, fontSize: 13, margin: "0 0 18px" }}>{report.id} · {report.when} · {org}</p>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(140px,1fr))", gap: 12, marginBottom: 18 }}>
                  {[
                    ["Total in sheet", report.total, T.text],
                    ["Attempted", report.attempted, T.accent],
                    ["Delivered", report.delivered, T.green],
                    ["Read", report.read, T.accent2],
                    ["Failed", report.failed, T.red],
                    ["Skipped (bad rows)", report.skipped, T.amber],
                  ].map(([l, v, c]) => (
                    <Card key={l} style={{ padding: 16 }}><Stat value={v} label={l} color={c} /></Card>
                  ))}
                </div>

                <Card style={{ marginBottom: 18 }}>
                  <div style={{ fontWeight: 700, marginBottom: 12 }}>Why messages failed</div>
                  {Object.keys(report.reasons).length === 0 && <div style={{ color: T.dim, fontSize: 13 }}>No failures 🎉</div>}
                  {Object.entries(report.reasons).map(([label, count]) => {
                    const meta = FAIL_REASONS.find((f) => f.label === label);
                    const w = Math.round((count / report.failed) * 100);
                    return (
                      <div key={label} style={{ marginBottom: 14 }}>
                        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 4 }}>
                          <span>{label}</span><span style={{ color: T.dim }}>{count} · {w}%</span>
                        </div>
                        <div style={{ height: 8, background: T.bg, borderRadius: 999, overflow: "hidden", marginBottom: 4 }}>
                          <div style={{ width: w + "%", height: "100%", background: T.red, opacity: 0.85 }} />
                        </div>
                        <div style={{ fontSize: 12, color: T.dim }}>💡 Fix: {meta?.fix}</div>
                      </div>
                    );
                  })}
                </Card>

                <Card>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
                    <div style={{ fontWeight: 700 }}>Per-contact log</div>
                    <Btn onClick={exportCSV}>⬇ Export CSV</Btn>
                  </div>
                  <div style={{ maxHeight: 280, overflowY: "auto" }}>
                    {report.rows.map((r, i) => (
                      <div key={i} style={{ display: "flex", justifyContent: "space-between", gap: 10, padding: "8px 4px", borderBottom: `1px solid ${T.line}`, fontSize: 13, flexWrap: "wrap" }}>
                        <span>{r.name || "—"} <span style={{ color: T.dim }}>· {r.phone}</span></span>
                        <span>
                          <Badge tone={r.status === "delivered" ? "ok" : "bad"}>{r.status}</Badge>
                          {r.reason && <span style={{ color: T.dim, fontSize: 12, marginLeft: 8 }}>{r.reason.label}</span>}
                        </span>
                      </div>
                    ))}
                    {report.skippedRows.map((r, i) => (
                      <div key={"s" + i} style={{ display: "flex", justifyContent: "space-between", gap: 10, padding: "8px 4px", borderBottom: `1px solid ${T.line}`, fontSize: 13 }}>
                        <span>{r.name || "—"} <span style={{ color: T.dim }}>· {r.phone}</span></span>
                        <span><Badge tone="warn">skipped</Badge> <span style={{ color: T.dim, fontSize: 12, marginLeft: 8 }}>Invalid number in sheet</span></span>
                      </div>
                    ))}
                  </div>
                </Card>
              </>
            )}
          </>
        )}

        {/* ---------- SETTINGS ---------- */}
        {view === "settings" && (
          <>
            <h1 style={{ fontSize: 22, fontWeight: 800, margin: "0 0 4px" }}>WhatsApp API settings</h1>
            <p style={{ color: T.dim, fontSize: 13.5, margin: "0 0 20px" }}>
              Connect your Meta WhatsApp Business (Cloud API) account. Each workspace/org keeps its own keys.
            </p>
            <div style={{ display: "flex", gap: 18, flexWrap: "wrap", alignItems: "flex-start" }}>
              <Card style={{ flex: "1 1 380px", minWidth: 320 }}>
                <Field label="Permanent Access Token" hint="From Meta Business Settings → System Users → Generate Token (scopes: whatsapp_business_messaging, whatsapp_business_management).">
                  <div style={{ display: "flex", gap: 8 }}>
                    <input style={inputStyle} type={showToken ? "text" : "password"} placeholder="EAAG…"
                      value={creds.token} onChange={(e) => setCreds({ ...creds, token: e.target.value })} />
                    <Btn style={{ padding: "8px 12px" }} onClick={() => setShowToken(!showToken)}>{showToken ? "Hide" : "Show"}</Btn>
                  </div>
                </Field>
                <Field label="Phone Number ID" hint="Meta Developer App → WhatsApp → API Setup → 'Phone number ID' (NOT the phone number itself).">
                  <input style={inputStyle} placeholder="e.g. 123456789012345" value={creds.phoneId}
                    onChange={(e) => setCreds({ ...creds, phoneId: e.target.value })} />
                </Field>
                <Field label="WhatsApp Business Account ID (WABA ID)" hint="Same API Setup screen — used to manage and submit message templates.">
                  <input style={inputStyle} placeholder="e.g. 987654321098765" value={creds.wabaId}
                    onChange={(e) => setCreds({ ...creds, wabaId: e.target.value })} />
                </Field>
                <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                  <Btn primary onClick={() => {}}>{credsOk ? "✓ Connected" : "Save & verify"}</Btn>
                  {credsOk && <Badge tone="ok">Live mode ready</Badge>}
                </div>
                <div style={{ fontSize: 11.5, color: T.dim, marginTop: 14, lineHeight: 1.5 }}>
                  🔒 In production these keys are stored encrypted on the server per organization — never in the browser.
                </div>
              </Card>
              <Card style={{ flex: "1 1 320px", minWidth: 300 }}>
                <div style={{ fontWeight: 700, marginBottom: 10 }}>Where do I get these? (5-min setup)</div>
                {[
                  ["1", "Go to business.facebook.com and create / open your Meta Business Portfolio."],
                  ["2", "Open developers.facebook.com → My Apps → Create App → type 'Business' → add the WhatsApp product."],
                  ["3", "In the app, open WhatsApp → API Setup. Copy the Phone Number ID and WABA ID shown there."],
                  ["4", "For a token that never expires: Business Settings → Users → System Users → Add → Generate Token → select your app → tick whatsapp_business_messaging + whatsapp_business_management."],
                  ["5", "Paste all three values here and click Save & verify. We send a test API call to confirm."],
                ].map(([n, t]) => (
                  <div key={n} style={{ display: "flex", gap: 10, marginBottom: 12, fontSize: 13, lineHeight: 1.5 }}>
                    <div style={{ width: 22, height: 22, borderRadius: "50%", background: T.grad, color: "#06101e", fontWeight: 800, fontSize: 12, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>{n}</div>
                    <div>{t}</div>
                  </div>
                ))}
                <div style={{ fontSize: 12, color: T.amber, lineHeight: 1.5 }}>
                  ⚠️ The temporary token on the API Setup page expires in 24h — always use a System User token for production.
                </div>
              </Card>
            </div>
          </>
        )}

        <footer style={{ marginTop: 30, fontSize: 11, color: "#4d5a72", display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
          <span>Demo build · sending simulated · production uses WhatsApp Cloud API templates & webhooks</span>
          <span>Built by <a href="https://nexzen.me" target="_blank" rel="noreferrer" style={{ color: T.accent2, textDecoration: "none", fontWeight: 700 }}>NexZen</a> — We Build. You Scale.</span>
        </footer>
      </main>
    </div>
  );
}
