import { defineConfig } from "vite";
import solid from "vite-plugin-solid";
import tailwindcss from "@tailwindcss/vite";

/**
 * Vite config for AMOR v2 web UI.
 *
 * Notes
 * -----
 * * The dev server proxies /api, /v1, and /mcp to the FastAPI process
 *   running on :8000.  In Docker compose this is the gateway /
 *   ``app:8000`` upstream.  Locally we point at host:8000.
 * * ``server.watch.usePolling: true`` is mandatory for WSL2 + Docker
 *   bind-mount HMR — chokidar's native filesystem events from a
 *   Windows host don't propagate into the container's mount.  Vite
 *   docs call this out explicitly:
 *   https://vite.dev/config/server-options.html#server-watch
 * * ``build.outDir`` lands in ``web_ui/v2/dist/`` (Vite default
 *   relative to this config file's parent).  FastAPI mounts that
 *   path at ``/static/v2`` and reads ``manifest.json`` to wire the
 *   hashed entry script into the Jinja2 template.
 */
export default defineConfig({
  plugins: [solid(), tailwindcss()],

  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    watch: {
      usePolling: true,
      interval: 300,
    },
    hmr: {
      clientPort: 5173,
    },
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: false,
        ws: false,
      },
      "/v1": {
        target: "http://localhost:8000",
        changeOrigin: false,
      },
      "/mcp": {
        target: "http://localhost:8000",
        changeOrigin: false,
      },
    },
  },

  build: {
    outDir: "dist",
    emptyOutDir: true,
    manifest: true,
    sourcemap: true,
    target: "es2022",
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name].[hash].js",
        chunkFileNames: "assets/[name].[hash].js",
        assetFileNames: "assets/[name].[hash][extname]",
        manualChunks: {
          // Code-mode-only chunks; chat-mode entry stays under budget
          highlight: ["highlight.js"],
        },
      },
    },
  },

});
