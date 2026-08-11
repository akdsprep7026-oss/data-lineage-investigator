import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // API only. Browser document navigations / hard refreshes of
      // /investigations/:id must receive the SPA, not backend JSON.
      "/investigations": {
        target: "http://127.0.0.1:8002",
        bypass(req) {
          if (req.headers.accept?.includes("text/html")) {
            return "/index.html";
          }
        },
      },
      "/health": "http://127.0.0.1:8002",
    },
  },
});
