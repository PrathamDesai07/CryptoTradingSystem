import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig } from "vite";

const backend = process.env.CTS_BACKEND_ORIGIN || "http://127.0.0.1:8000";
const apiBase = process.env.CTS_API_BASE || "";

// index.html loads app.js and config.js as classic (non-module) scripts so the
// source can also be served directly by the backend in bundled mode. Vite does
// not copy such scripts into the build, so emit them next to index.html. The
// deployed frontend may live on a different origin than the API, so config.js is
// generated from CTS_API_BASE (empty keeps same-origin URLs).
function renderConfigJs() {
  return [
    "// Generated at build time by vite.config.js from the CTS_API_BASE environment",
    "// variable. Empty means same-origin URLs: the Vite dev proxy in development,",
    "// or the backend process itself when frontend_serving_enabled is true.",
    `window.CTS_API_BASE = ${JSON.stringify(apiBase)};`,
    "",
  ].join("\n");
}

function includeClassicScripts() {
  const scripts = ["app.js", "config.js"];
  let root = process.cwd();
  return {
    name: "include-classic-scripts",
    configResolved(config) {
      root = config.root;
    },
    generateBundle() {
      for (const file of scripts) {
        const source = file === "config.js" ? renderConfigJs() : readFileSync(resolve(root, file));
        this.emitFile({ type: "asset", fileName: file, source });
      }
    },
  };
}

export default defineConfig({
  plugins: [includeClassicScripts()],
  server: {
    port: 3000,
    strictPort: true,
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/ws": { target: backend, ws: true, changeOrigin: true },
    },
  },
});
