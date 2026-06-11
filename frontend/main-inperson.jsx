// Entry for the phone-first in-person surface (inperson.html). Served by
// FastAPI for the event.surpluslayer.com host. A dedicated entry means the phone
// bundle never pulls the desktop pipeline App, and vice versa.
import React from "react";
import ReactDOM from "react-dom/client";

import BookApp from "./BookApp.jsx";
import InPersonApp from "./InPersonApp.jsx";
import { initAnalytics } from "./lib/analytics.js";

initAnalytics();

// The event hosts now serve the BookApp surface (Today · Add · Book) — the
// capture flow lives in its Add tab. The legacy in-person surface stays
// reachable at /legacy (and keeps powering /guest) while it's retired.
function wantsLegacy() {
  try {
    const p = window.location.pathname || "";
    return p === "/legacy" || p.startsWith("/legacy/")
        || p === "/guest" || p.startsWith("/guest/");
  } catch { return false; }
}

const Root = wantsLegacy() ? InPersonApp : BookApp;

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
