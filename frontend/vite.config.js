import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig } from "vite";

const backend = process.env.CTS_BACKEND_ORIGIN || "http://127.0.0.1:8000";

// index.html loads app.js and config.js as classic (non-module) scripts so the
// source can also be served directly by the backend in bundled mode. Vite does
// not copy such scripts into the build, so emit them verbatim next to index.html.
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
        this.emitFile({
          type: "asset",
          fileName: file,
          source: readFileSync(resolve(root, file)),
        });
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
