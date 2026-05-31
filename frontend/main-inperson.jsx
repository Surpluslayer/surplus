// Entry for the phone-first in-person surface (inperson.html). Served by
// FastAPI for the app.surpluslayer.com host. A dedicated entry means the phone
// bundle never pulls the desktop pipeline App, and vice versa.
import React from "react";
import ReactDOM from "react-dom/client";

import InPersonApp from "./InPersonApp.jsx";
import { initAnalytics } from "./lib/analytics.js";

initAnalytics();

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <InPersonApp />
  </React.StrictMode>
);
