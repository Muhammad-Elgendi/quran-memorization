import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { noiseSuppressionAudioWorkletVitePlugin } from "@workadventure/noise-suppression/vite";
import { patchDtlnSkipWebGpu } from "./vite-plugins/patch-dtln-skip-webgpu.js";

const proxyTarget = process.env.VITE_PROXY_TARGET || "http://localhost:8000";

export default defineConfig({
  plugins: [
    vue(),
    patchDtlnSkipWebGpu(),
    noiseSuppressionAudioWorkletVitePlugin(),
  ],
  test: {
    environment: "node",
    include: ["tests/**/*.test.js"],
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: proxyTarget,
        changeOrigin: true,
        ws: true,
      },
      "/health": {
        target: proxyTarget,
        changeOrigin: true,
      },
    },
  },
});
