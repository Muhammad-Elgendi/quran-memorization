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

/**
 * Continuous PCM capture via AudioWorklet → 16 kHz s16le chunks.
 */
export async function startPcmCapture({ chunkMs = 250, onChunk, onError } = {}) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });

  const audioContext = new AudioContext();
  await audioContext.audioWorklet.addModule("/pcm-worklet.js");

  const source = audioContext.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(audioContext, "pcm-capture-processor", {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [1],
    channelCount: 1,
    processorOptions: {
      targetRate: 16000,
      chunkMs,
    },
  });

  node.port.onmessage = (event) => {
    if (onChunk && event.data) {
      onChunk(event.data);
    }
  };
  node.onprocessorerror = (err) => {
    if (onError) onError(err);
  };

  // Keep the graph alive without monitoring (muted).
  const mute = audioContext.createGain();
  mute.gain.value = 0;
  source.connect(node);
  node.connect(mute);
  mute.connect(audioContext.destination);

  return {
    stream,
    audioContext,
    node,
    async stop() {
      try {
        node.port.onmessage = null;
        source.disconnect();
        node.disconnect();
        mute.disconnect();
      } catch {
        /* ignore */
      }
      stream.getTracks().forEach((t) => t.stop());
      if (audioContext.state !== "closed") {
        await audioContext.close();
      }
    },
  };
}
