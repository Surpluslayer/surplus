import { defineConfig } from "vite";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  // dev server: proxy API calls to the local FastAPI on :8000 so the React
  // app can use relative paths in BOTH dev and prod. In production, FastAPI
  // serves the built frontend at the same origin so the proxy isn't needed.
  server: {
    port: 5173,
    proxy: {
      "/events":   { target: "http://localhost:8000", changeOrigin: true },
      "/webhooks": { target: "http://localhost:8000", changeOrigin: true },
      "/api":      { target: "http://localhost:8000", changeOrigin: true },
      "/docs":     { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Multi-page: two HTML shells from one build, sharing the hashed /assets.
    //   inperson.html -> the Book (every product host; the desktop
    //   pipeline shell was retired with the events side).
    // FastAPI serves the landing on join./www/apex, the Book elsewhere.
    rollupOptions: {
      input: {
        inperson: resolve(__dirname, "inperson.html"),
      },
      output: {
        // Keep BookApp in its own hashed chunk (BookApp-*.js). The desktop
        // entry no longer dynamically imports it, so without this it inlines
        // into the event entry and /api/health can't fingerprint the shipped
        // book bundle (frontend_book_bundle / frontend_has_redesign go null).
        manualChunks(id) {
          if (id.includes("/BookApp.jsx")) return "BookApp";
        },
      },
    },
  },
});
