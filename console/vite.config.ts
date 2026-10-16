/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// In development the browser calls /api on the Vite server, which forwards to the API
// Gateway (no CORS needed). Production builds call VITE_API_BASE directly; that origin
// is allowed by the orchestrator's CORS list and by firebase.json's CSP.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: env.VITE_API_BASE,
          changeOrigin: true,
          secure: true,
          rewrite: (path) => path.replace(/^\/api/, ""),
        },
      },
    },
    // Firebase Hosting only serves directories inside firebase/ (where firebase.json lives).
    build: { outDir: "../firebase/hosting", emptyOutDir: true, sourcemap: false },
    test: { environment: "node" },
  };
});
