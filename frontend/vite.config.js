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
    //   index.html    -> desktop pipeline   (surpluslayer.com)
    //   inperson.html -> phone-first capture (app.surpluslayer.com)
    // FastAPI picks which shell to serve per Host (backend/main.py).
    rollupOptions: {
      input: {
        main: resolve(__dirname, "index.html"),
        inperson: resolve(__dirname, "inperson.html"),
      },
    },
  },
});
