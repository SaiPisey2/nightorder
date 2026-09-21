import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base=/ui/ so the built app serves same-origin from FastAPI's /ui mount.
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  server: {
    proxy: {
      // dev server proxies API calls to the FastAPI backend
      "^/(projects|runs|gates|gate-action|agents|incidents|health|catalog)": {
        target: "http://localhost:8400",
        changeOrigin: true,
      },
    },
  },
});
