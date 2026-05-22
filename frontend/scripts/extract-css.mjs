import fs from "fs";
const s = fs.readFileSync(new URL("../App.jsx", import.meta.url), "utf8");
const start = s.indexOf("const CSS = `");
if (start < 0) throw new Error("const CSS not found");
const bodyStart = start + "const CSS = `".length;
const end = s.indexOf("\n`;", bodyStart);
if (end < 0) throw new Error("end backtick not found");
const css = s.slice(bodyStart, end);
const extra = `
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
const out =
  "export const SURPLUS_APP_CSS = `" + css + extra + "\n`;\n";
fs.writeFileSync(new URL("../surplusTheme.js", import.meta.url), out);
console.log("Wrote surplusTheme.js", out.length);
