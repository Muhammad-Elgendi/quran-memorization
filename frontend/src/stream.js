/** Build WebSocket URL for the memorization stream endpoint. */
export function streamWsUrl() {
  const base = import.meta.env.VITE_API_BASE_URL || "";
  if (base) {
    const u = new URL(base);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    u.pathname = `${u.pathname.replace(/\/$/, "")}/api/memorization/stream`;
    u.search = "";
    u.hash = "";
    return u.toString();
  }
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/api/memorization/stream`;
}

export { startPcmCapture, startProcessedStream } from "./audio/capture-service.js";
