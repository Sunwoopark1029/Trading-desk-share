import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Localhost only. /api is forwarded to the FastAPI backend (keys stay there).
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  preview: { host: "127.0.0.1", port: 5173, strictPort: true },
});
