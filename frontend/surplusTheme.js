export const SURPLUS_APP_CSS = `
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Newsreader:opsz,wght@6..72,400;6..72,500&display=swap');
* { box-sizing:border-box; margin:0; padding:0; }
.root {
  --bg:#f4f5f7; --panel:#ffffff; --panel-2:#fafbfc; --panel-3:#f1f3f6;
  --line:#e6e8eb; --line-soft:#eef0f2;
  --ink:#1b1e22; --ink-dim:#5b616a; --ink-faint:#99a0a8;
  --acc:#2f6df6; --acc-deep:#2257d6; --acc-soft:#eaf1fe; --acc-light:#86abf9;
  --ok:#1f9d62; --ok-soft:#e7f5ee; --no:#c0433d; --no-soft:#fbeceb;
  --build:#2f6df6; --hire:#3f7fd6; --op:#cf5fa6;
  --shadow:0 8px 30px rgba(20,23,28,0.06); --shadow-sm:0 3px 14px rgba(20,23,28,0.05);
  --r-card:14px; --r-panel:12px; --r-el:10px; --r-pill:999px;
  --gray-soft:#f0f1f4;
  --font-ui:'Inter',system-ui,sans-serif;
  --font-display:'Newsreader',Georgia,'Times New Roman',serif;
  font-family:var(--font-ui); background:var(--bg);
  color:var(--ink); min-height:100vh; padding:24px;
  background-image:none;
}
.frame { max-width:1080px; margin:0 auto; }
.topbar {
  display:flex; align-items:center; justify-content:space-between;
  padding:15px 22px; background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-card); box-shadow:var(--shadow-sm);
  flex-wrap:wrap; gap:14px; margin-bottom:18px;
}
.brand { display:flex; align-items:center; gap:12px; isolation:isolate; }
.brand-logo { width:44px; height:44px; display:block; object-fit:contain;
  mix-blend-mode:multiply; filter:drop-shadow(0 8px 18px rgba(47,109,246,0.14)); }
.brand-text { min-height:44px; display:flex; flex-direction:column; justify-content:center; gap:1px; }
.brand-name { font-family:'Inter',system-ui,sans-serif; font-weight:800;
  letter-spacing:-0.05em; font-size:1.85rem; line-height:1; color:var(--ink); }
.brand-sub { font-size:11px; color:var(--ink-faint); line-height:1.2; }
.live-badge { margin-left:14px; padding:4px 10px; border-radius:var(--r-pill);
  font-size:10.5px; font-weight:600; letter-spacing:0.02em; text-transform:uppercase;
  background:var(--acc-soft); color:var(--acc);
  border:1px solid rgba(47,109,246,0.18); }
.api-error { padding:10px 18px; background:#fff5f5; color:#b03030;
  border-bottom:1px solid #f3d6d6; font-size:13px; font-weight:500; }

/* FailureStrip: surfaces partial-pipeline failures (rate limits, source
   timeouts, no-matches) above the prospect list. Wired in by App.jsx
   FailureStrip component reading runResult.failures. */
.failure-strip { display:flex; flex-direction:column; gap:6px;
  margin:10px 0 6px; }
.failure-line { display:flex; gap:8px; align-items:flex-start;
  padding:8px 12px; border-radius:8px; font-size:12.5px;
  line-height:1.45; margin:0; }
.failure-line.failure-warn { background:#fff8ed; color:#8a5a00;
  border:1px solid #f1d9a3; }
.failure-line.failure-info { background:#f1f6ff; color:#1c4d9e;
  border:1px solid #c9dcfc; }
.failure-icon { flex:0 0 auto; font-size:14px; line-height:1.2; }
.failure-text { flex:1 1 auto; }
.signin-modal-backdrop {
  position:fixed; inset:0; z-index:1000;
  display:flex; align-items:center; justify-content:center;
  padding:24px; background:rgba(31,28,46,0.45);
}
.signin-modal {
  width:100%; max-width:400px; background:var(--panel);
  border:1px solid var(--line); border-radius:var(--r-card);
  box-shadow:var(--shadow); padding:28px 26px; text-align:center;
}
.signin-modal-title { font-size:18px; font-weight:700; letter-spacing:-0.02em; margin-bottom:8px; }
.signin-modal-sub { font-size:13px; line-height:1.55; color:var(--ink-dim); margin-bottom:22px; }
.signin-modal-cta {
  display:inline-flex; align-items:center; justify-content:center; gap:10px;
  width:100%; padding:13px 20px; border-radius:var(--r-pill); border:0;
  background:#0a66c2; color:white; font-family:inherit; font-weight:600;
  font-size:14px; cursor:pointer; transition:background 0.15s;
}
.signin-modal-cta:hover:not(:disabled) { background:#084e96; }
.signin-modal-cta:disabled { opacity:0.75; cursor:wait; }
.signin-modal-dismiss {
  margin-top:12px; background:transparent; border:0; color:var(--ink-faint);
  font-family:inherit; font-size:12px; cursor:pointer; padding:6px 10px;
}
.signin-modal-dismiss:hover { color:var(--ink-dim); }
.signin-modal-divider {
  display:flex; align-items:center; gap:10px; margin:18px 0 12px;
  color:var(--ink-faint); font-size:10.5px; text-transform:uppercase;
  letter-spacing:0.08em;
}
.signin-modal-divider::before, .signin-modal-divider::after {
  content:""; flex:1; height:1px; background:var(--line);
}
.signin-modal-secondary {
  display:block; width:100%; padding:10px 14px; border-radius:10px;
  background:transparent; color:var(--ink-dim); border:1px dashed var(--line);
  font-family:inherit; font-size:12.5px; cursor:pointer; text-align:center;
  transition:background 0.15s, color 0.15s, border-color 0.15s;
}
.signin-modal-secondary:hover {
  background:var(--acc-soft); color:var(--acc); border-color:var(--acc);
}
.signin-modal-triage { display:flex; flex-direction:column; gap:8px; text-align:left; }
.signin-modal-triage-hint {
  font-size:11.5px; color:var(--ink-faint); line-height:1.5; margin:0;
  text-align:center;
}
.signin-modal-input {
  width:100%; padding:10px 12px; border-radius:10px;
  border:1px solid var(--line); background:var(--panel);
  font-family:inherit; font-size:13.5px; color:var(--ink);
  box-sizing:border-box;
}
.signin-modal-input:focus { outline:none; border-color:var(--acc); }
.signin-modal-triage-cta {
  width:100%; padding:11px 16px; border-radius:var(--r-pill); border:0;
  background:var(--acc); color:white; font-family:inherit; font-weight:600;
  font-size:13px; cursor:pointer; transition:background 0.15s;
  margin-top:4px;
}
.signin-modal-triage-cta:hover:not(:disabled) { background:var(--acc-deep); }
.signin-modal-triage-cta:disabled { opacity:0.6; cursor:not-allowed; }
.signin-modal-cancel {
  background:none; border:0; padding:6px; cursor:pointer;
  font-family:inherit; font-size:11.5px; color:var(--ink-faint);
  text-align:center; text-decoration:underline;
}
.signin-modal-cancel:hover { color:var(--ink-dim); }
.signin-modal-err {
  padding:9px 11px; background:#fff5f5; color:#b03030;
  border:1px solid #ffd6d6; border-radius:8px;
  font-size:12px; line-height:1.4;
}
.rail { display:flex; gap:5px; flex-wrap:wrap; }
.rail-item { display:flex; align-items:center; gap:7px; background:transparent;
  border:1px solid transparent; color:var(--ink-faint); padding:7px 12px; cursor:pointer;
  font-family:inherit; font-size:11.5px; font-weight:500; border-radius:var(--r-pill);
  transition:all 0.18s; }
.rail-item:disabled { cursor:not-allowed; opacity:0.45; }
.rail-item:not(:disabled):hover { color:var(--acc); background:var(--acc-soft); }
.rail-item.active { color:#fff; background:var(--acc); border-color:var(--acc);
  box-shadow:0 4px 12px rgba(47,109,246,0.3); }
.rail-item.done { color:var(--ink-dim); }
.rail-dot { display:flex; }
.rail-label { font-size:11.5px; font-weight:500; }
.rail-idx { font-size:9px; opacity:0.6; }
.canvas { animation:fade 0.4s ease; }
@keyframes fade { from{opacity:0;transform:translateY(6px);} to{opacity:1;transform:none;} }
.stage { display:flex; flex-direction:column; gap:22px; }
.stage-head { max-width:560px; margin-bottom:2px; }
.stage-head h1 { font-family:var(--font-display); font-weight:500;
  font-size:clamp(1.5rem, 2.4vw, 1.95rem); line-height:1.18; letter-spacing:-0.01em;
  margin:0; color:var(--ink); }
.lede { font-size:13.5px; line-height:1.7; color:var(--ink-dim); }
.lede em { color:var(--acc); font-style:normal; font-weight:600; }
.form-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:20px 18px; display:flex; flex-direction:column; gap:11px; box-shadow:var(--shadow-sm); }
.card h3 { font-size:13px; font-weight:700; letter-spacing:-0.01em; display:flex;
  align-items:center; gap:9px; margin-bottom:4px; color:var(--ink); }
.card-num { width:21px; height:21px; background:var(--acc-soft); border-radius:7px;
  color:var(--acc); display:grid; place-items:center; font-size:11px; font-weight:700; }
.card label { font-size:10px; letter-spacing:0.04em; color:var(--ink-faint);
  text-transform:uppercase; font-weight:600; margin-top:4px; }
.card label strong { color:var(--acc); }
.hint { text-transform:none; letter-spacing:0; color:var(--ink-faint); font-weight:400; }
.text-in { background:var(--panel-2); border:1px solid var(--line); border-radius:var(--r-el);
  color:var(--ink); font-family:inherit; font-size:12.5px; padding:10px 12px; }
.text-in:focus { outline:none; border-color:var(--acc); background:#fff;
  box-shadow:0 0 0 3px var(--acc-soft); }
.chip-row { display:flex; flex-wrap:wrap; gap:7px; }
.chip { background:var(--panel-2); border:1px solid var(--line); color:var(--ink-dim);
  font-family:inherit; font-size:11.5px; font-weight:500; padding:7px 11px;
  border-radius:var(--r-pill); cursor:pointer; transition:all 0.15s; }
.chip:hover { border-color:var(--acc-light); color:var(--acc); }
.chip-on { background:var(--acc); border-color:var(--acc); color:#fff; font-weight:600;
  box-shadow:0 3px 10px rgba(47,109,246,0.25); }
.range-in { width:100%; accent-color:var(--acc); cursor:pointer; }
.topo-inline { font-size:10.5px; color:var(--ink-faint); display:flex; align-items:center; gap:5px; }
.derived { display:flex; gap:12px; margin-top:6px; padding-top:13px; border-top:1px dashed var(--line); }
.derived > div { flex:1; display:flex; flex-direction:column; gap:3px; }
.derived-k { font-size:9px; color:var(--ink-faint); text-transform:uppercase;
  letter-spacing:0.04em; font-weight:600; }
.derived-v { font-size:15px; color:var(--acc); font-weight:700; }
.stage-foot { display:flex; align-items:center; justify-content:space-between;
  border-top:1px solid var(--line); padding-top:20px; gap:16px; }
.foot-note { font-size:11.5px; color:var(--ink-faint); }
.btn-primary { background:var(--acc); color:#fff; border:none; font-family:inherit;
  font-size:12.5px; font-weight:700; padding:12px 20px; cursor:pointer; display:flex;
  align-items:center; gap:8px; letter-spacing:-0.01em; border-radius:var(--r-el);
  box-shadow:0 6px 16px rgba(47,109,246,0.3); transition:all 0.16s; white-space:nowrap; }
.btn-primary:hover { background:var(--acc-deep); transform:translateY(-1px);
  box-shadow:0 8px 20px rgba(47,109,246,0.38); }
.pipe-sources { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; }
.pipe-card { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:16px; box-shadow:var(--shadow-sm); }
.pipe-card-top { display:flex; align-items:center; gap:11px; margin-bottom:13px; color:var(--ink-dim); }
.pipe-card-icon { flex-shrink:0; display:block; object-fit:contain; }
.pipe-card-top > div { flex:1; }
.pipe-card-label { font-size:12.5px; color:var(--ink); font-weight:600; }
.pipe-card-note { font-size:10px; color:var(--ink-faint); }
.pipe-pct { font-size:13px; color:var(--acc); font-weight:700; }
.bar { height:5px; background:var(--panel-3); border-radius:var(--r-pill); overflow:hidden; }
.bar-fill { height:100%; background:linear-gradient(90deg,var(--acc-light),var(--acc));
  border-radius:var(--r-pill); transition:width 0.55s ease-out; }
.pipe-steps { display:flex; gap:10px; flex-wrap:wrap; }
.pipe-step { display:flex; align-items:center; gap:8px; font-size:11.5px; color:var(--ink-faint);
  border:1px solid var(--line); border-radius:var(--r-pill); padding:9px 14px;
  background:var(--panel); font-weight:500; transition:all 0.3s; }
.pipe-step.on { color:var(--ink); border-color:var(--acc-light); }
.pipe-step.complete { color:var(--acc); border-color:var(--acc); background:var(--acc-soft); }
.pipe-counter { display:flex; align-items:baseline; gap:14px; padding:22px;
  background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  box-shadow:var(--shadow-sm); }
.pipe-counter-num { font-size:46px; color:var(--acc); font-weight:800; letter-spacing:-0.02em; }
.pipe-counter-lbl { font-size:12px; color:var(--ink-dim); }
.agent-bar { display:flex; align-items:center; gap:20px; background:var(--panel);
  border:1px solid var(--line); border-radius:var(--r-card); padding:13px 18px;
  box-shadow:var(--shadow-sm); flex-wrap:wrap; }
.agent-bar-live { display:flex; align-items:center; gap:7px; font-size:11px; color:var(--ok);
  text-transform:uppercase; letter-spacing:0.05em; font-weight:700; }
.live-dot { width:7px; height:7px; border-radius:50%; background:var(--ok);
  animation:pulse 1.4s infinite; }
@keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.25;} }
.agent-stat { font-size:11.5px; color:var(--ink-faint); }
.agent-stat strong { color:var(--ink); font-size:13px; font-weight:700; }
.prospect-layout { display:grid; grid-template-columns:1.25fr 1fr; gap:16px; }
.prospect-list { background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-card); box-shadow:var(--shadow-sm); align-self:start; overflow:hidden; }
.list-head { display:grid; grid-template-columns:1fr auto auto; gap:14px; padding:13px 16px;
  border-bottom:1px solid var(--line); font-size:9px; letter-spacing:0.06em;
  text-transform:uppercase; color:var(--ink-faint); font-weight:700; }
.prospect-row { display:grid; grid-template-columns:1fr auto auto; gap:14px; align-items:center;
  width:100%; background:transparent; border:none; border-bottom:1px solid var(--line-soft);
  padding:13px 16px; cursor:pointer; font-family:inherit; text-align:left; transition:background 0.14s; }
.prospect-row:hover { background:var(--panel-2); }
.prospect-row.sel { background:var(--acc-soft); box-shadow:inset 3px 0 0 var(--acc); }
.prospect-row.dim { opacity:0.55; }
.pr-name { display:flex; flex-direction:column; gap:3px; }
.pr-name-main { font-size:12.5px; color:var(--ink); font-weight:600; display:flex;
  align-items:center; gap:7px; }
.pr-role { font-size:10px; color:var(--ink-faint); }
.pr-signal { display:flex; gap:9px; }
.sig { font-size:10px; color:var(--ink-dim); display:flex; align-items:center; gap:3px; }
.pr-status { display:flex; align-items:center; justify-content:flex-end; }
.st-tag { font-size:8px; letter-spacing:0.03em; text-transform:uppercase; padding:3px 7px;
  border-radius:var(--r-pill); font-weight:700; }
.st-approved { background:var(--ok-soft); color:var(--ok); }
.st-rsvp { background:var(--ok-soft); color:var(--ok); }
.st-contacted { background:#e7eefb; color:var(--hire); }
.st-below { background:var(--no-soft); color:var(--no); }
.side-tag { font-size:8px; letter-spacing:0.03em; text-transform:uppercase; padding:3px 7px;
  border-radius:var(--r-pill); font-weight:700; white-space:nowrap; }
.side-build { color:var(--build); background:rgba(47,109,246,0.1); }
.side-hire { color:var(--hire); background:rgba(63,127,214,0.1); }
.side-op { color:var(--op); background:rgba(207,95,166,0.1); }
.locked-prospects { position:relative; overflow:hidden; }
.locked-prospects-rows { filter:blur(5px); opacity:0.55; pointer-events:none;
  user-select:none; }
.locked-prospects-overlay { position:absolute; inset:0; display:flex;
  flex-direction:column; align-items:center; justify-content:center; gap:11px;
  text-align:center; padding:18px;
  background:linear-gradient(to bottom, rgba(255,255,255,0) 0%, var(--panel) 50%); }
.locked-prospects-overlay > svg { color:var(--acc); }
.locked-prospects-count { font-size:13px; font-weight:600; color:var(--ink-dim); }
.unlock-cta { display:inline-flex; align-items:center; gap:6px;
  background:var(--acc); color:#fff; border:0; border-radius:var(--r-pill);
  padding:9px 17px; font-size:12.5px; font-weight:700; cursor:pointer;
  transition:background 0.15s; }
.unlock-cta:hover { background:var(--acc-deep); }
.threshold-note { font-size:10px; color:var(--ink-faint); padding:13px 16px;
  display:flex; align-items:center; gap:8px; }
.threshold-line { flex:1; height:1px;
  background:repeating-linear-gradient(90deg,var(--line) 0 4px,transparent 4px 8px); }
.prospect-side { display:flex; flex-direction:column; gap:16px; }
.prospect-detail { background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-card); padding:20px; box-shadow:var(--shadow-sm); }
.pd-head { display:flex; justify-content:space-between; align-items:flex-start;
  gap:12px; margin-bottom:15px; }
.pd-head h3 { font-size:18px; font-weight:700; letter-spacing:-0.01em; }
.pd-head p { font-size:11px; color:var(--ink-faint); margin-top:3px; }
.score-badge { font-size:18px; font-weight:800; padding:7px 12px; border-radius:var(--r-el);
  border:1px solid; }
.score-badge.ok { color:var(--ok); border-color:var(--ok); background:var(--ok-soft); }
.score-badge.no { color:var(--no); border-color:var(--no); background:var(--no-soft); }
.pd-section { margin-top:15px; }
.pd-label { font-size:9px; letter-spacing:0.08em; text-transform:uppercase;
  color:var(--ink-faint); margin-bottom:8px; font-weight:700; }
.pd-link { font-size:10px; line-height:1.4; color:var(--hire); font-weight:500;
  word-break:break-all; }
.pd-link:hover { text-decoration:underline; }
.pd-reason { font-size:12px; line-height:1.65; color:var(--ink-dim); }
.vectors { display:flex; flex-direction:column; gap:7px; }
.vectors > div { display:flex; gap:10px; align-items:baseline; }
.vec-k { font-size:9px; text-transform:uppercase; letter-spacing:0.05em; color:var(--ink-faint);
  width:42px; flex-shrink:0; font-weight:700; }
.vec-v { font-size:11.5px; color:var(--ink); font-weight:500; }
.outreach { background:var(--panel-2); border:1px solid var(--line); border-radius:var(--r-panel);
  padding:14px; display:flex; flex-direction:column; gap:9px; }
.outreach p { font-size:11px; line-height:1.65; color:var(--ink-dim); }
.outreach.muted p { color:var(--ink-faint); }
.outreach-status { font-size:9px; color:var(--ok); display:flex; align-items:center; gap:5px;
  text-transform:uppercase; letter-spacing:0.04em; font-weight:700; }
.outreach-tag { font-size:9px; color:var(--ink-faint); display:flex; align-items:center; gap:5px;
  text-transform:uppercase; letter-spacing:0.03em; margin-top:2px; font-weight:600; }
.msg-label { font-size:9px !important; color:var(--ink-faint) !important; text-transform:uppercase;
  letter-spacing:0.05em; font-weight:700; margin-top:8px; }
.msg-body { font-size:11.5px !important; color:var(--ink) !important; line-height:1.55 !important;
  white-space:pre-wrap; background:#fff; border:1px solid var(--line); border-radius:8px;
  padding:10px 12px; }
.msg-edit { width:100%; font-family:'Inter',system-ui,sans-serif; font-size:11.5px;
  color:var(--ink); line-height:1.55; background:#fff; border:1px solid var(--line);
  border-radius:8px; padding:10px 12px; resize:vertical; box-sizing:border-box;
  transition:border-color 0.15s, box-shadow 0.15s; }
.msg-edit:focus { outline:none; border-color:var(--acc);
  box-shadow:0 0 0 3px rgba(47,109,246,0.12); }
.msg-edit-long { min-height:120px; }
.msg-warn { color:#b03030; font-weight:600; }
.below-threshold-warn { font-size:11px; color:#8a6a1f; background:#fff8e1;
  border:1px solid #f3e2a8; border-radius:8px; padding:9px 12px; margin:0 0 4px 0;
  line-height:1.5; }
.btn-reset { margin-left:auto; background:transparent; border:none; color:var(--acc);
  font-family:inherit; font-size:9px; font-weight:600; cursor:pointer;
  text-transform:uppercase; letter-spacing:0.04em; padding:0; }
.btn-reset:hover { text-decoration:underline; }
.muted-text { color:var(--ink-faint); font-size:11px; font-style:italic; }
.prov-tag { margin-left:8px; padding:2px 7px; background:var(--acc-soft); color:var(--acc);
  border-radius:var(--r-pill); font-size:9px; font-weight:700; letter-spacing:0.04em;
  text-transform:uppercase; }
.send-row { display:flex; gap:8px; margin-top:6px; }
.btn-send { flex:1; padding:9px 12px; border-radius:8px; border:1px solid var(--acc);
  background:var(--acc); color:#fff; font-family:inherit; font-size:11.5px; font-weight:600;
  cursor:pointer; display:inline-flex; align-items:center; justify-content:center; gap:6px;
  transition:all 0.18s; }
.btn-send:hover:not(:disabled) { transform:translateY(-1px); box-shadow:0 4px 12px rgba(47,109,246,0.25); }
.btn-send:disabled { opacity:0.5; cursor:not-allowed; }
.btn-send-dm { background:#fff; color:var(--acc); }
.send-result { margin-top:6px; font-size:10.5px; padding:7px 10px; border-radius:6px;
  display:flex; align-items:center; gap:6px; font-weight:600; }
.send-result.ok { background:#e9f7ec; color:#1f7a3e; }
.send-result.err { background:#fff5f5; color:#b03030; }
.send-result.muted { background:var(--panel-2); color:var(--ink-faint); font-weight:500; }
.agent-feed { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:18px; box-shadow:var(--shadow-sm); }
.feed-scroll { display:flex; flex-direction:column; gap:6px; max-height:176px; overflow:hidden; }
.feed-row { display:flex; align-items:center; gap:9px; font-size:10px; padding:7px 10px;
  background:var(--panel-2); border-radius:var(--r-el); border-left:2px solid var(--line);
  animation:fade 0.3s ease; }
.feed-icon { display:flex; color:var(--ink-faint); }
.feed-text { color:var(--ink-dim); font-weight:500; }
.feed-name { margin-left:auto; color:var(--ink); font-weight:600; }
.fr-sent { border-left-color:var(--ink-faint); }
.fr-open { border-left-color:var(--hire); }
.fr-open .feed-icon { color:var(--hire); }
.fr-rsvp { border-left-color:var(--ok); }
.fr-rsvp .feed-icon, .fr-rsvp .feed-text { color:var(--ok); }
.fr-wait { border-left-color:var(--ink-faint); opacity:0.7; }
.fr-pending { opacity:0.5; border-left-style:dashed; }
.match-layout { display:flex; flex-direction:column; gap:16px; }
.match-side { display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:14px; }
.graph-wrap { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:12px; box-shadow:var(--shadow-sm); position:relative; width:100%; }
.graph-wrap--main { min-height:520px; }
.graph-wrap--loading { margin-top:8px; }
.graph-wrap-title { margin:0 0 10px; font-size:10px; font-weight:700; letter-spacing:0.08em;
  text-transform:uppercase; color:var(--ink-faint); }
.radar-graph-wrap { position:relative; width:100%; min-height:inherit; }
.radar-graph-canvas { display:block; width:100%; height:100%; min-height:inherit;
  border-radius:12px; background:var(--panel-2); }
.radar-graph-empty { position:absolute; inset:0; display:grid; place-items:center;
  font-size:13px; color:var(--ink-dim); pointer-events:none; }
.radar-graph-loading { position:absolute; inset:0; display:grid; place-items:center;
  font-size:13px; color:var(--acc); font-weight:600; background:rgba(255,255,255,0.72);
  border-radius:12px; pointer-events:none; }
.radar-graph-reset { position:absolute; top:10px; right:10px; z-index:2;
  font-family:inherit; font-size:10px; font-weight:600; padding:6px 10px;
  border-radius:999px; border:1px solid var(--line); background:var(--panel);
  color:var(--ink-dim); cursor:pointer; }
.radar-graph-reset:hover { color:var(--acc); border-color:var(--acc-light); }
.graph { width:100%; height:auto; display:block; }
.hull { fill:rgba(47,109,246,0.035); stroke:var(--line); stroke-dasharray:3 4; }
.hull-label { fill:var(--ink-faint); font-size:9px; letter-spacing:0.1em; text-anchor:middle;
  font-family:'Inter',sans-serif; text-transform:uppercase; font-weight:700; }
.edge { stroke-linecap:round; }
.edge-sym { stroke:var(--acc); stroke-width:2; opacity:0.5; }
.edge-aff { stroke:var(--ink-faint); stroke-width:1; opacity:0.35; stroke-dasharray:2 3; }
.edge-cross { opacity:0.13; }
.node { stroke-width:1.5; }
.node-side-build { fill:rgba(47,109,246,0.12); stroke:var(--build); }
.node-side-hire { fill:rgba(63,127,214,0.12); stroke:var(--hire); }
.node-side-op { fill:rgba(207,95,166,0.12); stroke:var(--op); }
.node-init { fill:var(--ink); font-size:10px; font-weight:700; text-anchor:middle;
  font-family:'Inter',sans-serif; }
.node-name { fill:var(--ink-faint); font-size:9px; text-anchor:middle;
  font-family:'Inter',sans-serif; font-weight:500; }
.legend { display:flex; flex-wrap:wrap; gap:16px; padding:10px 6px 4px; }
.legend span { font-size:9px; color:var(--ink-faint); display:flex; align-items:center; gap:5px;
  text-transform:uppercase; letter-spacing:0.03em; font-weight:600; }
.legend i { width:14px; height:0; display:inline-block; }
.lg-sym { border-top:2px solid var(--acc); }
.lg-aff { border-top:1px dashed var(--ink-faint); }
.lg-build { width:9px; height:9px; border-radius:50%; background:rgba(47,109,246,0.15);
  border:1.5px solid var(--build); }
.lg-hire { width:9px; height:9px; border-radius:50%; background:rgba(63,127,214,0.15);
  border:1.5px solid var(--hire); }
.sym-panel { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:16px; box-shadow:var(--shadow-sm); }
.sym-pair { background:var(--panel-2); border:1px solid var(--line); border-radius:var(--r-panel);
  padding:11px 12px; margin-bottom:8px; }
.sym-pair:last-child { margin-bottom:0; }
.sym-names { font-size:12px; color:var(--ink); font-weight:600; display:flex;
  align-items:center; gap:7px; }
.sym-link { color:var(--acc); }
.sym-w { margin-left:auto; font-size:11px; color:var(--acc); font-weight:800; }
.sym-flow { font-size:9px; color:var(--ink-faint); margin-top:5px; }
.sym-flow span { color:var(--acc); }
.tables-panel { display:flex; flex-direction:column; gap:12px; }
.table-card { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:15px; box-shadow:var(--shadow-sm); }
.table-card-head { display:flex; align-items:center; gap:8px; font-size:11.5px; font-weight:700;
  letter-spacing:-0.01em; margin-bottom:10px; }
.table-dot { width:9px; height:9px; border-radius:3px; background:var(--acc); }
.table-count { margin-left:auto; font-size:10px; color:var(--ink-faint);
  border:1px solid var(--line); border-radius:var(--r-pill); padding:2px 8px; font-weight:600; }
.table-guest { display:flex; justify-content:space-between; align-items:center; font-size:11px;
  padding:6px 0; color:var(--ink-dim); border-bottom:1px dotted var(--line); }
.table-rationale { font-size:10px; color:var(--ink-faint); line-height:1.55; margin-top:9px; }
.roi-top { display:grid; grid-template-columns:1fr 1.2fr; gap:16px; }
.roi-hero { background:linear-gradient(145deg,#4f86f8,#2f6df6); color:#fff; padding:24px;
  border-radius:var(--r-card); display:flex; flex-direction:column; gap:7px;
  box-shadow:0 10px 30px rgba(47,109,246,0.35); }
.roi-hero-label { font-size:11px; letter-spacing:0.05em; text-transform:uppercase;
  font-weight:700; opacity:0.85; }
.roi-hero-num { font-size:62px; font-weight:800; line-height:1; letter-spacing:-0.02em; }
.roi-hero-sub { font-size:11px; opacity:0.85; }
.roi-funnel { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:20px; display:flex; flex-direction:column; gap:12px; box-shadow:var(--shadow-sm); }
.rf-row { display:flex; align-items:center; gap:12px; }
.rf-k { font-size:11px; color:var(--ink-dim); width:74px; font-weight:500; }
.rf-bar { flex:1; height:18px; background:var(--panel-3); border-radius:var(--r-pill); overflow:hidden; }
.rf-fill { height:100%; background:linear-gradient(90deg,var(--acc-light),var(--acc));
  border-radius:var(--r-pill); }
.rf-v { font-size:13px; color:var(--ink); width:32px; text-align:right; font-weight:800; }
.rf-foot { font-size:10px; color:var(--ink-faint); margin-top:2px; }
.roi-li { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  padding:18px 20px; margin-top:16px; box-shadow:var(--shadow-sm);
  display:flex; flex-direction:column; gap:14px; }
.roi-li-head { display:flex; align-items:baseline; gap:10px; }
.roi-li-title { font-size:12px; font-weight:700; color:var(--ink); letter-spacing:0.02em; }
.roi-li-sub { font-size:10px; color:var(--ink-faint); }
.roi-li-tiles { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.roi-li-tile { background:var(--panel-3); border:1px solid var(--line-soft);
  border-radius:var(--r-card); padding:14px 16px; display:flex; flex-direction:column; gap:4px; }
.roi-li-k { font-size:10px; letter-spacing:0.05em; text-transform:uppercase;
  color:var(--ink-dim); font-weight:700; }
.roi-li-v { font-size:30px; font-weight:800; color:var(--acc); letter-spacing:-0.02em; line-height:1.05; }
.roi-li-d { font-size:10px; color:var(--ink-faint); }
.roi-li-steps { display:flex; flex-direction:column; gap:8px; }
.rls-row { display:flex; align-items:center; gap:12px; }
.rls-k { font-size:11px; color:var(--ink-dim); width:92px; font-weight:500; }
.rls-bar { flex:1; height:12px; background:var(--panel-3); border-radius:var(--r-pill); overflow:hidden; }
.rls-fill { height:100%; background:linear-gradient(90deg,var(--acc-light),var(--acc));
  border-radius:var(--r-pill); }
.rls-v { font-size:12px; color:var(--ink); width:32px; text-align:right; font-weight:800; }
.ledger { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card);
  box-shadow:var(--shadow-sm); overflow:hidden; }
.ledger-head { display:grid; grid-template-columns:1.6fr 0.7fr 1.6fr 0.8fr; gap:12px;
  padding:13px 18px; border-bottom:1px solid var(--line); font-size:9px; letter-spacing:0.06em;
  text-transform:uppercase; color:var(--ink-faint); font-weight:700; }
.ledger-row { display:grid; grid-template-columns:1.6fr 0.7fr 1.6fr 0.8fr; gap:12px;
  padding:14px 18px; border-bottom:1px solid var(--line-soft); align-items:center; }
.led-guest { display:flex; flex-direction:column; gap:2px; }
.led-name { font-size:12.5px; color:var(--ink); font-weight:600; }
.led-co { font-size:10px; color:var(--ink-faint); }
.led-outcome { display:flex; flex-direction:column; gap:4px; }
.led-pill { font-size:9px; text-transform:uppercase; letter-spacing:0.03em; padding:3px 8px;
  width:fit-content; border-radius:var(--r-pill); font-weight:700; }
.led-pill-won { background:var(--ok-soft); color:var(--ok); }
.led-pill-partial { background:var(--acc-soft); color:var(--acc); }
.led-pill-lost { background:var(--no-soft); color:var(--no); }
.led-detail { font-size:10px; color:var(--ink-faint); }
.led-value { font-size:13px; font-weight:800; color:var(--ink); text-align:right; }
.led-lost .led-value { color:var(--ink-faint); }
.led-lost { opacity:0.65; }
.ledger-foot { display:flex; justify-content:space-between; padding:15px 18px;
  font-size:11px; color:var(--ink-dim); text-transform:uppercase; letter-spacing:0.04em; font-weight:600; }
.ledger-total { font-size:17px; font-weight:800; color:var(--acc); letter-spacing:-0.01em; }
/* Sponsor column : added when the event carries ≥1 sponsor. The grid
   template grows by one slot before "Verified value". */
.ledger--sponsored .ledger-head,
.ledger--sponsored .ledger-row {
  grid-template-columns:1.6fr 0.7fr 1.6fr 1.0fr 0.8fr;
}
.led-sponsor { font-size:11.5px; color:var(--ink); font-weight:600; }
/* Sponsor bar : the inline "+ Add sponsor" + chip row that sits above
   the value graph on the Matching screen. Low-weight UI by design : a
   chip-row with an Add button, not a card. */
.sponsor-bar { margin:0 0 14px; }
.sponsor-bar-row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.sponsor-chip { border:1px solid var(--line); background:var(--panel-2);
  color:var(--ink); border-radius:var(--r-pill); padding:5px 11px;
  font-size:12px; font-weight:600; cursor:pointer; display:inline-flex;
  align-items:center; gap:6px; }
.sponsor-chip:hover { border-color:var(--acc-light); color:var(--acc); }
.sponsor-chip.active { background:var(--acc); border-color:var(--acc); color:#fff; }
.sponsor-chip.active .sponsor-chip-tier { background:rgba(255,255,255,0.18); color:#fff; }
.sponsor-chip-tier { font-size:9px; text-transform:uppercase; letter-spacing:0.04em;
  font-weight:700; padding:1px 6px; border-radius:var(--r-pill);
  background:var(--acc-soft); color:var(--acc); }
.sponsor-add-btn { border:1px dashed var(--line); background:transparent;
  color:var(--ink-dim); border-radius:var(--r-pill); padding:5px 11px;
  font-size:12px; cursor:pointer; font-weight:600; }
.sponsor-add-btn:hover { border-color:var(--acc); color:var(--acc); }
.sponsor-form { margin-top:10px; padding:12px; background:var(--panel-2);
  border:1px solid var(--line); border-radius:var(--r-panel); }
.sponsor-form-head { display:flex; gap:8px; margin-bottom:8px; }
.sponsor-form-head .text-in { margin:0; flex:1; }
.sponsor-form .sponsor-tier { max-width:140px; }
.sponsor-form-buyer { display:grid; grid-template-columns:repeat(2, 1fr);
  gap:8px; }
.sponsor-form-buyer .text-in { margin:0; }
.sponsor-form-actions { display:flex; gap:8px; margin-top:10px; align-items:center; }
.sponsor-form-actions .btn-primary { padding:6px 14px; font-size:12px; }
.sponsor-form-delete { color:var(--no); }
/* Sponsor match block on the matching screen */
.sponsor-match-block { margin-bottom:10px; }
.sponsor-match-block:last-child { margin-bottom:0; }
.sponsor-match-name { font-size:11.5px; font-weight:700; color:var(--ink);
  margin:6px 0 4px; display:flex; align-items:center; gap:8px; }
.sponsor-tier-pill { font-size:9px; text-transform:uppercase;
  letter-spacing:0.04em; padding:2px 7px; border-radius:var(--r-pill);
  background:var(--acc-soft); color:var(--acc); font-weight:700; }
@media (max-width:880px) {
  .form-grid, .pipe-sources { grid-template-columns:1fr; }
  .prospect-layout, .roi-top, .roi-li-tiles { grid-template-columns:1fr; }
  .match-side { grid-template-columns:1fr; }
  .ledger-head, .ledger-row { grid-template-columns:1.4fr 1.4fr 0.7fr; }
  .ledger-head span:nth-child(2), .ledger-row > span:nth-child(2) { display:none; }
  .stage-head h1 { font-size:1.35rem; }
}

/* ─── User menu in topbar ─────────────────────────────────── */
.user-menu { position:relative; margin-left:auto; }
.topbar-signin {
  margin-left:auto;
  padding:6px 14px; border-radius:var(--r-pill);
  background:#0a66c2; color:white; border:0;
  font-family:inherit; font-size:13px; font-weight:600;
  cursor:pointer; transition:background 0.12s;
}
.topbar-signin:hover { background:#084e96; }
.topbar-mode-switch {
  padding:6px 12px; background:transparent; border:1px solid var(--line);
  border-radius:999px; font-family:inherit; font-size:11.5px;
  color:var(--ink-dim); cursor:pointer; transition:all 0.15s;
  margin-right:6px;
}
.topbar-mode-switch:hover { color:var(--acc); border-color:var(--acc); background:var(--acc-soft); }
.user-pill {
  display:inline-flex; align-items:center; gap:8px;
  padding:5px 12px 5px 5px;
  background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-pill); cursor:pointer;
  font-family:inherit; font-size:13px; color:var(--ink);
  transition:border-color 0.12s, background 0.12s;
}
.user-pill:hover { border-color:var(--acc); background:var(--acc-soft); }
.user-avatar-img { width:26px; height:26px; border-radius:50%; object-fit:cover; }
.user-avatar-initials {
  width:26px; height:26px; border-radius:50%;
  background:var(--acc); color:white;
  display:flex; align-items:center; justify-content:center;
  font-size:11px; font-weight:600;
}
.user-name {
  max-width:160px; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; font-weight:500;
}
.user-dropdown {
  position:absolute; top:calc(100% + 8px); right:0; min-width:240px;
  background:var(--panel); border:1px solid var(--line);
  border-radius:12px; box-shadow:0 8px 24px rgba(15,15,30,0.10);
  padding:6px; z-index:30;
}
.user-dropdown-head {
  padding:12px 14px 10px;
  border-bottom:1px solid var(--line-soft);
  margin-bottom:4px;
}
.user-dropdown-name { font-weight:600; font-size:13.5px; color:var(--ink); }
.user-dropdown-email { font-size:12px; color:var(--ink-faint); margin-top:2px; }
.user-dropdown-status {
  display:inline-flex; align-items:center; gap:6px;
  font-size:11.5px; color:var(--ink-dim); margin-top:8px;
}
.status-dot { width:6px; height:6px; border-radius:50%; }
.status-dot.ok { background:#10b981; }
.status-dot.stale { background:#f59e0b; }
.user-dropdown-action {
  display:flex; align-items:center; gap:8px; width:100%;
  padding:9px 12px; background:transparent; border:0; border-radius:8px;
  font-family:inherit; font-size:13px; color:var(--ink-dim);
  cursor:pointer; text-align:left;
}
.user-dropdown-action:hover { background:var(--panel-3); color:var(--ink); }
.user-dropdown-toggle {
  display:flex; align-items:center; gap:8px; width:100%;
  padding:9px 12px; border-radius:8px;
  font-size:13px; color:var(--ink-dim);
}
.user-dropdown-toggle .toggle-label { display:flex; flex-direction:column; gap:1px; flex:1; }
.user-dropdown-toggle .toggle-sub { font-size:11px; color:var(--ink-faint); }
.switch {
  position:relative; flex-shrink:0; width:34px; height:20px;
  border-radius:999px; border:0; padding:0; cursor:pointer;
  background:var(--line); transition:background .15s ease;
}
.switch[aria-checked="true"] { background:#10b981; }
.switch[disabled] { opacity:.55; cursor:default; }
.switch .knob {
  position:absolute; top:2px; left:2px; width:16px; height:16px;
  border-radius:50%; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,.2);
  transition:transform .15s ease;
}
.switch[aria-checked="true"] .knob { transform:translateX(14px); }
textarea.text-in { min-height:72px; resize:vertical; line-height:1.5; }
.luma-import-row { display:flex; gap:8px; align-items:stretch; flex-wrap:wrap; }
.luma-import-row .text-in { flex:1; min-width:200px; }
.luma-quick {
  display:flex; align-items:center; gap:10px; flex-wrap:wrap;
  padding:10px 14px; background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-el); box-shadow:var(--shadow-sm);
}
.luma-quick-icon { color:var(--acc); flex:0 0 auto; }
.luma-quick-label { font-size:13px; font-weight:600; color:var(--ink); white-space:nowrap; }
.luma-quick-input { flex:1 1 240px; min-width:240px; }
.luma-quick-btn { padding:8px 14px; flex:0 0 auto; }
.luma-quick-hint { font-size:12px; color:var(--ink-faint); white-space:nowrap; }
.luma-ok-banner {
  display:flex; align-items:flex-start; gap:7px; padding:10px 12px; margin-top:10px;
  border-radius:var(--r-el); background:var(--ok-soft); color:var(--ok);
  border:1px solid rgba(31,157,107,0.22); font-size:12px; line-height:1.55;
}
.triage-topbar-actions { display:flex; align-items:center; gap:12px; margin-left:auto; flex-wrap:wrap; }
.card-num svg { display:block; }
.topbar-luma {
  display:flex; align-items:center; gap:6px; margin-left:auto;
  padding:4px 6px 4px 10px; background:var(--panel-2);
  border:1px solid var(--line); border-radius:var(--r-pill);
}
.topbar-luma-icon { color:var(--acc); flex:0 0 auto; }
.topbar-luma-input {
  border:0; background:transparent; outline:none; font-family:inherit;
  font-size:12.5px; color:var(--ink); width:180px; padding:4px 2px;
}
.topbar-luma-input::placeholder { color:var(--ink-faint); }
.topbar-luma-input:disabled { opacity:0.6; cursor:wait; }
.topbar-luma-go {
  background:var(--acc); color:#fff; border:0; border-radius:var(--r-pill);
  font-family:inherit; font-size:12px; font-weight:600;
  padding:5px 12px; cursor:pointer; transition:background 0.12s;
  display:inline-flex; align-items:center; gap:5px; min-width:32px;
  justify-content:center;
}
.topbar-luma-go:hover:not(:disabled) { background:var(--acc-deep); }
.topbar-luma-go:disabled { opacity:0.55; cursor:not-allowed; }

`;
