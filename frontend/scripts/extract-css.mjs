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
.luma-ok-banner {
  display:flex; align-items:flex-start; gap:7px; padding:10px 12px; margin-top:10px;
  border-radius:var(--r-el); background:var(--ok-soft); color:var(--ok);
  border:1px solid rgba(31,157,107,0.22); font-size:12px; line-height:1.55;
}
.triage-topbar-actions { display:flex; align-items:center; gap:12px; margin-left:auto; flex-wrap:wrap; }
.card-num svg { display:block; }
`;
const out =
  "export const SURPLUS_APP_CSS = `" + css + extra + "\n`;\n";
fs.writeFileSync(new URL("../surplusTheme.js", import.meta.url), out);
console.log("Wrote surplusTheme.js", out.length);
