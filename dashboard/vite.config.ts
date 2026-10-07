import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API (src/api.py) runs on uvicorn; in development Vite forwards the
// API's paths to it, so the dashboard calls the same paths in both modes
const API = "http://127.0.0.1:8000";
const API_PATHS = ["/health", "/summary", "/line", "/parts", "/inspection-queue", "/alerts", "/stations", "/twin", "/plant", "/products", "/analyst"];

export default defineConfig(({ command }) => ({
  plugins: [react()],
  // The built dashboard is served by the API at /dashboard/
  base: command === "build" ? "/dashboard/" : "/",
  server: {
    port: 5173,
    proxy: Object.fromEntries(API_PATHS.map((path) => [path, API])),
  },
  // One bundle (mostly React and Recharts, ~190 kB gzipped) is fine for a
  // local dashboard
  build: { chunkSizeWarningLimit: 1000 },
}));
