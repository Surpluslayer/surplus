// DemoApp.jsx — the public, no-sign-in walkthrough served at
// event.surpluslayer.com/demo.
//
// A guided coach-mark tour that walks a first-time visitor through the product
// as one continuous story: capture someone at an event (InPersonApp), watch
// them flow into the relationship book (BookApp), and see the proactive
// notification that brings you back. Everything runs on seed data fetched from
// POST /api/demo/start — no LinkedIn, no real sends (the demo session has no
// connected account, so the backend 402s every send path).
//
// A persistent banner offers the one real action: "Sign in with LinkedIn to
// use it for real." Connecting lands the visitor on the regular app with the
// standard onboarding armed — converting them is the whole point.
import React, { useEffect, useMemo, useState } from "react";
import {
  QrCode, Sparkles, Send, Bell, ArrowRight, ArrowLeft, Check,
  MessageSquare, Briefcase, Rocket, PartyPopper,
} from "lucide-react";

import { api } from "./lib/api.js";
import { ensureNotifyPermission, notifyDevice } from "./lib/notify.js";

// LinkedIn brand mark (lucide dropped brand icons). Uses the PNG the app
// already ships at /linkedin-icon.png so the demo matches the rest of the UI.
function LiMark({ size = 15 }) {
  return (
    <img src="/linkedin-icon.png" alt="" width={size} height={size}
         style={{ display: "inline-block", verticalAlign: "-2px", borderRadius: 3 }} />
  );
}

// Top-level navigation to the connect-first (free, no-paywall on the event
// host) LinkedIn flow. The callback returns the user to "/" — the regular
// event.surpluslayer.com app — with onboarding_status armed to "active", so a
// brand-new sign-in lands straight in the real onboarding.
function signInWithLinkedIn() {
  window.location.href = "/api/auth/linkedin/start-redirect";
}

function initialsOf(name = "") {
  return name.split(/\s+/).filter(Boolean).slice(0, 2)
    .map((w) => w[0].toUpperCase()).join("") || "·";
}

// ── persistent demo banner ───────────────────────────────────────────────
function DemoBanner() {
  return (
    <div className="demo-banner">
      <span className="demo-banner-tag">Demo</span>
      <span className="demo-banner-text">
        You're exploring with sample data. Nothing is sent.
      </span>
      <button className="demo-banner-cta" onClick={signInWithLinkedIn}>
        <LiMark size={15} /> Sign in to use it for real
      </button>
    </div>
  );
}

// ── the phone frame every step renders inside ────────────────────────────
function Phone({ label, children }) {
  return (
    <div className="phone">
      <div className="phone-notch" />
      <div className="phone-top">{label}</div>
      <div className="phone-screen">{children}</div>
    </div>
  );
}

// ── person card reused across screens ────────────────────────────────────
function PersonRow({ p, sub }) {
  return (
    <div className="p-row">
      <div className="p-avatar">{initialsOf(p.name)}</div>
      <div className="p-meta">
        <div className="p-name">{p.name}</div>
        <div className="p-sub">{sub || p.headline}</div>
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
// step screens
// ════════════════════════════════════════════════════════════════════════

function ScreenWelcome() {
  return (
    <div className="hero">
      <img src="/surplus-logo.png" alt="surplus" className="hero-logo"
           onError={(e) => { e.currentTarget.style.display = "none"; }} />
      <h1 className="hero-title">Turn everyone you meet into a relationship that compounds.</h1>
      <p className="hero-sub">
        surplus captures the people you meet at events and quietly keeps the
        relationship warm. Here's the 60-second tour.
      </p>
    </div>
  );
}

function ScreenScan({ person }) {
  const [scanned, setScanned] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setScanned(true), 1100);
    return () => clearTimeout(t);
  }, []);
  return (
    <Phone label="Capture">
      {!scanned ? (
        <div className="scan-pad">
          <div className="scan-box"><QrCode size={56} /></div>
          <div className="scan-hint">Scanning badge…</div>
        </div>
      ) : (
        <div className="resolved">
          <div className="resolved-pill"><Check size={14} /> Resolved</div>
          <div className="p-avatar lg">{initialsOf(person.name)}</div>
          <div className="resolved-name">{person.name}</div>
          <div className="resolved-head">{person.headline}</div>
          <div className="resolved-co"><Briefcase size={14} /> {person.company}</div>
        </div>
      )}
    </Phone>
  );
}

function ScreenDraft({ person, onSend, sent }) {
  return (
    <Phone label="Warm draft">
      <div className="draft-wrap">
        <PersonRow p={person} />
        <div className="draft-note-label">
          <Sparkles size={13} /> What you talked about
        </div>
        <div className="draft-note">{person.note}</div>
        <div className="draft-box">{person.draft}</div>
        {sent ? (
          <div className="sent-toast"><Check size={15} /> Demo — nothing was sent</div>
        ) : (
          <button className="send-btn" onClick={onSend}>
            <Send size={15} /> Send invite
          </button>
        )}
      </div>
    </Phone>
  );
}

function ScreenToday({ people, eventLabel }) {
  return (
    <Phone label="Today">
      <div className="today-head">
        <div className="today-title">Needs outreach</div>
        <div className="today-sub">{people.length} from {eventLabel}</div>
      </div>
      <div className="today-list">
        {people.map((p) => (
          <PersonRow key={p.key} p={p}
                     sub={`${p.company} · just captured`} />
        ))}
      </div>
    </Phone>
  );
}

function ScreenRelationship({ person }) {
  return (
    <Phone label={person.name}>
      <div className="rel">
        <PersonRow p={person} />
        <div className="rel-timeline">
          <div className="rel-tl-item">
            <span className="rel-dot" /> Met at the mixer
          </div>
          <div className="rel-tl-item">
            <span className="rel-dot" /> Captured to your book
          </div>
        </div>
        <div className="rel-suggest-label">
          <MessageSquare size={13} /> Suggested follow-up
        </div>
        <div className="rel-suggest">{person.draft}</div>
      </div>
    </Phone>
  );
}

function ScreenNotify({ update, state, onEnable }) {
  return (
    <Phone label="Updates">
      <div className="notify-wrap">
        {state === "shown" || state === "denied" || state === "default" ? (
          <div className="notif-card">
            <div className="notif-icon">
              {update.kind === "launch" ? <Rocket size={18} /> : <Briefcase size={18} />}
            </div>
            <div className="notif-body">
              <div className="notif-title">{update.headline}</div>
              <div className="notif-detail">{update.detail}</div>
            </div>
          </div>
        ) : (
          <div className="notify-pitch">
            <div className="notify-bell"><Bell size={40} /></div>
            <div className="notify-copy">
              surplus watches for moments worth reaching out — a job change, a
              launch — so you never miss the right time.
            </div>
            <button className="enable-btn" onClick={onEnable}>
              <Bell size={15} /> Turn on notifications
            </button>
          </div>
        )}
        {state === "denied" && (
          <div className="notify-foot">Notifications are off in your browser —
            here's what one looks like.</div>
        )}
      </div>
    </Phone>
  );
}

function ScreenFinish() {
  return (
    <div className="hero">
      <div className="finish-emoji"><PartyPopper size={40} /></div>
      <h1 className="hero-title">That's surplus.</h1>
      <p className="hero-sub">
        Sign in with LinkedIn to use it for real — your captures, your book,
        your follow-ups. It's free to start.
      </p>
      <button className="finish-cta" onClick={signInWithLinkedIn}>
        <LiMark size={18} /> Sign in with LinkedIn
      </button>
      <div className="finish-note">You'll land in your own workspace next.</div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════
// the tour
// ════════════════════════════════════════════════════════════════════════

export default function DemoApp() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [step, setStep] = useState(0);
  const [sent, setSent] = useState(false);
  const [notifyState, setNotifyState] = useState("idle");

  const load = () => {
    setError(null); setData(null);
    api.demoStart()
      .then((r) => setData(r?.demo || null))
      .catch((e) => setError(e?.message || "Could not start the demo."));
  };
  useEffect(() => { load(); }, []);

  const people = data?.people || [];
  const featured = people[0];
  // The person carrying the noteworthy signal drives the notification step.
  const updatePerson = useMemo(
    () => people.find((p) => p.update) || null, [people]);

  const steps = useMemo(() => {
    if (!featured) return [];
    return [
      { caption: "Welcome", render: () => <ScreenWelcome /> },
      {
        caption: "Scan a badge QR or paste a LinkedIn URL. surplus resolves who they are on the spot.",
        render: () => <ScreenScan person={featured} />,
      },
      {
        caption: "We draft a note in your voice from what you talked about. Edit it, or send.",
        render: () => (
          <ScreenDraft person={featured} sent={sent}
                       onSend={() => setSent(true)} />
        ),
      },
      {
        caption: "Everyone you capture flows into your book. Today surfaces who to follow up with.",
        render: () => (
          <ScreenToday people={people} eventLabel={data?.event_label || "the event"} />
        ),
      },
      {
        caption: "Open anyone to see your history together and a ready-to-send follow-up.",
        render: () => <ScreenRelationship person={featured} />,
      },
      {
        caption: "surplus nudges you when there's a real reason to reach out. Turn on notifications to see one.",
        render: () => (
          <ScreenNotify
            update={updatePerson?.update || {
              kind: "job_change",
              headline: "Someone in your book just changed jobs",
              detail: "A warm congrats now is the perfect reason to reconnect.",
            }}
            state={notifyState}
            onEnable={async () => {
              const perm = await ensureNotifyPermission();
              // Fire a real device notification (the OS shows it when the tab
              // isn't focused / on mobile). The in-app card below is the
              // reliable visual either way.
              const u = updatePerson?.update;
              if (perm === "granted" && u) {
                notifyDevice(u.headline, { body: u.detail });
                setNotifyState("shown");
              } else {
                setNotifyState(perm === "granted" ? "shown" : perm);
              }
            }}
          />
        ),
      },
      { caption: "Done", render: () => <ScreenFinish /> },
    ];
  }, [featured, people, data, sent, notifyState, updatePerson]);

  if (error) {
    return (
      <div className="demo-root">
        <style>{DEMO_CSS}</style>
        <div className="demo-center">
          <div className="hero">
            <h1 className="hero-title">Couldn't start the demo</h1>
            <p className="hero-sub">{error}</p>
            <button className="finish-cta" onClick={load}>Try again</button>
          </div>
        </div>
      </div>
    );
  }
  if (!data || !steps.length) {
    return (
      <div className="demo-root">
        <style>{DEMO_CSS}</style>
        <div className="demo-center"><div className="demo-spinner" /></div>
      </div>
    );
  }

  const isLast = step === steps.length - 1;
  const cur = steps[step];

  return (
    <div className="demo-root">
      <style>{DEMO_CSS}</style>
      <DemoBanner />
      <div className="demo-center">
        <div className="demo-stage">{cur.render()}</div>

        {step > 0 && !isLast && (
          <div className="coach"><span className="coach-arrow" />{cur.caption}</div>
        )}

        <div className="demo-nav">
          <button className="nav-back" disabled={step === 0}
                  onClick={() => setStep((s) => Math.max(0, s - 1))}>
            <ArrowLeft size={16} /> Back
          </button>
          <div className="dots">
            {steps.map((_, i) => (
              <span key={i} className={"dot" + (i === step ? " on" : "")}
                    onClick={() => setStep(i)} />
            ))}
          </div>
          {isLast ? (
            <button className="nav-next primary" onClick={signInWithLinkedIn}>
              <LiMark size={16} /> Sign in
            </button>
          ) : (
            <button className="nav-next" onClick={() => setStep((s) => s + 1)}>
              {step === 0 ? "Take the tour" : "Next"} <ArrowRight size={16} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ── styles (self-contained so the demo never leaks into BookApp CSS) ──────
const DEMO_CSS = `
.demo-root{min-height:100vh;background:#f6f7f9;color:#14171c;
  font-family:Inter,system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased;display:flex;flex-direction:column}
.demo-banner{position:sticky;top:0;z-index:20;display:flex;align-items:center;
  gap:10px;padding:9px 14px;background:#14171c;color:#fff;font-size:13px}
.demo-banner-tag{font-size:11px;font-weight:700;letter-spacing:.04em;
  text-transform:uppercase;background:#2f6df6;color:#fff;border-radius:6px;
  padding:2px 7px}
.demo-banner-text{opacity:.8;flex:1;min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.demo-banner-cta{display:inline-flex;align-items:center;gap:6px;border:0;
  cursor:pointer;background:#fff;color:#14171c;font-weight:600;font-size:13px;
  padding:6px 12px;border-radius:999px;white-space:nowrap}
.demo-banner-cta:hover{background:#e9edf3}
.demo-center{flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:20px;padding:28px 16px 40px}
.demo-stage{display:flex;justify-content:center;width:100%}

.phone{width:300px;background:#fff;border:1px solid #e3e7ec;border-radius:30px;
  box-shadow:0 18px 50px rgba(20,23,28,.13);overflow:hidden;position:relative}
.phone-notch{position:absolute;top:9px;left:50%;transform:translateX(-50%);
  width:88px;height:6px;border-radius:99px;background:#e3e7ec}
.phone-top{padding:22px 18px 10px;font-weight:600;font-size:15px;
  border-bottom:1px solid #f0f2f5}
.phone-screen{padding:16px 16px 22px;min-height:340px}

.hero{max-width:440px;text-align:center;padding:10px}
.hero-logo{height:34px;margin:0 auto 18px;display:block}
.hero-title{font-family:Newsreader,Georgia,serif;font-size:30px;line-height:1.18;
  font-weight:600;margin:0 0 14px}
.hero-sub{font-size:15px;line-height:1.55;color:#576070;margin:0 auto;max-width:380px}
.finish-emoji,.notify-bell{color:#2f6df6}
.finish-emoji{margin-bottom:6px}
.finish-cta,.enable-btn,.send-btn,.nav-next.primary{display:inline-flex;
  align-items:center;gap:8px;border:0;cursor:pointer;background:#14171c;color:#fff;
  font-weight:600;border-radius:999px}
.finish-cta{font-size:16px;padding:12px 24px;margin-top:22px}
.finish-cta:hover{background:#000}
.finish-note{margin-top:12px;font-size:13px;color:#8a92a0}

.p-row{display:flex;align-items:center;gap:11px;padding:9px 0}
.p-avatar{width:38px;height:38px;border-radius:50%;background:#2f6df6;color:#fff;
  font-weight:700;font-size:14px;display:flex;align-items:center;justify-content:center;
  flex:0 0 auto}
.p-avatar.lg{width:64px;height:64px;font-size:22px;margin:6px auto}
.p-meta{min-width:0}
.p-name{font-weight:600;font-size:14px}
.p-sub{font-size:12.5px;color:#6b7280;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;max-width:200px}

.scan-pad{display:flex;flex-direction:column;align-items:center;gap:16px;
  padding-top:48px;color:#9aa3b1}
.scan-box{width:120px;height:120px;border:2px dashed #cfd6df;border-radius:18px;
  display:flex;align-items:center;justify-content:center;color:#2f6df6}
.scan-hint{font-size:13px}
.resolved{text-align:center;padding-top:14px}
.resolved-pill{display:inline-flex;align-items:center;gap:5px;font-size:12px;
  font-weight:600;color:#0a7d33;background:#e7f6ec;border-radius:999px;padding:3px 10px}
.resolved-name{font-weight:700;font-size:17px;margin-top:8px}
.resolved-head{font-size:13px;color:#576070;margin-top:4px;padding:0 8px}
.resolved-co{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;
  color:#6b7280;margin-top:10px}

.draft-wrap{display:flex;flex-direction:column;gap:10px}
.draft-note-label,.rel-suggest-label{display:flex;align-items:center;gap:6px;
  font-size:11.5px;font-weight:600;color:#8a92a0;text-transform:uppercase;
  letter-spacing:.03em;margin-top:4px}
.draft-note{font-size:13px;color:#576070;font-style:italic}
.draft-box,.rel-suggest{background:#f3f6fb;border:1px solid #e3e9f2;border-radius:12px;
  padding:11px 13px;font-size:13px;line-height:1.5;color:#1d2533}
.send-btn{font-size:14px;padding:11px;justify-content:center;margin-top:4px}
.sent-toast{display:flex;align-items:center;justify-content:center;gap:7px;
  background:#e7f6ec;color:#0a7d33;font-weight:600;font-size:13.5px;
  border-radius:12px;padding:11px;margin-top:4px}

.today-head{padding-bottom:8px;border-bottom:1px solid #f0f2f5;margin-bottom:4px}
.today-title{font-weight:700;font-size:15px}
.today-sub{font-size:12.5px;color:#8a92a0;margin-top:2px}
.today-list{display:flex;flex-direction:column;divide:1px}
.today-list .p-row{border-bottom:1px solid #f4f6f8}

.rel-timeline{margin:12px 0 6px}
.rel-tl-item{display:flex;align-items:center;gap:9px;font-size:13px;color:#576070;
  padding:5px 0}
.rel-dot{width:8px;height:8px;border-radius:50%;background:#2f6df6;flex:0 0 auto}

.notify-wrap{display:flex;flex-direction:column;gap:14px;padding-top:8px}
.notify-pitch{display:flex;flex-direction:column;align-items:center;gap:14px;
  text-align:center;padding:24px 8px}
.notify-copy{font-size:13.5px;line-height:1.5;color:#576070}
.enable-btn{font-size:14px;padding:11px 18px}
.notif-card{display:flex;gap:12px;background:#fff;border:1px solid #e3e7ec;
  border-radius:16px;padding:14px;box-shadow:0 8px 24px rgba(20,23,28,.10);
  animation:slidein .35s ease}
.notif-icon{width:38px;height:38px;border-radius:10px;background:#eef3ff;color:#2f6df6;
  display:flex;align-items:center;justify-content:center;flex:0 0 auto}
.notif-title{font-weight:600;font-size:14px}
.notif-detail{font-size:12.5px;color:#6b7280;margin-top:3px;line-height:1.45}
.notify-foot{font-size:12px;color:#8a92a0;text-align:center}
@keyframes slidein{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}

.coach{max-width:340px;text-align:center;font-size:14px;line-height:1.5;
  color:#1d2533;background:#fff;border:1px solid #e3e7ec;border-radius:14px;
  padding:13px 16px;box-shadow:0 6px 18px rgba(20,23,28,.07);position:relative}
.coach-arrow{position:absolute;top:-7px;left:50%;transform:translateX(-50%) rotate(45deg);
  width:12px;height:12px;background:#fff;border-left:1px solid #e3e7ec;
  border-top:1px solid #e3e7ec}

.demo-nav{display:flex;align-items:center;gap:14px;width:100%;max-width:340px;
  justify-content:space-between}
.nav-back,.nav-next{display:inline-flex;align-items:center;gap:6px;border:1px solid #d6dae1;
  background:#fff;color:#14171c;font-weight:600;font-size:14px;padding:9px 16px;
  border-radius:999px;cursor:pointer}
.nav-back:disabled{opacity:.35;cursor:default}
.nav-next{border-color:#14171c;background:#14171c;color:#fff}
.nav-next:hover{background:#000}
.dots{display:flex;gap:7px}
.dot{width:7px;height:7px;border-radius:50%;background:#d3d8df;cursor:pointer;
  transition:transform .15s}
.dot.on{background:#2f6df6;transform:scale(1.35)}

.demo-spinner{width:30px;height:30px;border-radius:50%;border:3px solid #e3e7ec;
  border-top-color:#2f6df6;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

@media (max-width:520px){
  .demo-banner-text{display:none}
  .hero-title{font-size:25px}
}
`;
