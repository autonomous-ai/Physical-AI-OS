import fs from "fs";
import path from "path";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const versionFile = path.resolve(__dirname, "../VERSION_WEB");
const webVersion = fs.existsSync(versionFile)
  ? fs.readFileSync(versionFile, "utf-8").trim()
  : "dev";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, __dirname, "LAMP_");
  const proxy = env.LAMP_PROXY || process.env.LAMP_PROXY;
  return {
    define: {
      __WEB_VERSION__: JSON.stringify(webVersion),
    },
    plugins: [react(), tailwindcss()],
    server: {
      proxy: proxy ? {
        // ws: true so the shell WebSocket (/api/system/shell) is proxied.
        "/api": { target: proxy, ws: true, changeOrigin: true },
        "/hw":  { target: proxy, ws: true, changeOrigin: true },
      } : undefined,
    },
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
  };
});
