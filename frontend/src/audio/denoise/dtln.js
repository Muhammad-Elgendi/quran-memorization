import { bypassUntilReady } from "../capture.js";

/**
 * DTLN denoiser via @workadventure/noise-suppression (16 kHz).
 *
 * Runs LiteRT in the AudioWorklet on wasm/CPU only. WebGPU is unavailable in
 * AudioWorkletGlobalScope (no navigator), so GPU machines use the same portable
 * CPU path as CPU-only devices — intentional and supported.
 *
 * @param {AudioContext} audioContext
 * @param {{ bypassUntilReady?: boolean }} [options]
 */
export async function createDtlnDenoise(audioContext, options = {}) {
  const { createNoiseSuppressionAudioWorklet } = await import(
    "@workadventure/noise-suppression/audio-worklet"
  );

  const worklet = await createNoiseSuppressionAudioWorklet(audioContext, {
    bypassUntilReady: options.bypassUntilReady ?? bypassUntilReady(),
  });

  const ready = worklet.ready.then((message) => {
    const details = message?.modelDetails;
    console.info("[audio] DTLN ready (wasm/cpu — works on GPU and CPU devices)", {
      threads: details?.threads ?? false,
      numThreads: details?.numThreads,
    });
    return message;
  });

  return {
    node: worklet.node,
    ready,
    dispose() {
      try {
        worklet.dispose();
      } catch {
        /* ignore */
      }
    },
  };
}
