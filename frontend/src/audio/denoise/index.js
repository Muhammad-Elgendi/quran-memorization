import { createNativeDenoise } from "./native.js";
import { createDtlnDenoise } from "./dtln.js";
import { createFastenhancerStream } from "./fastenhancer.js";

export { createNativeDenoise, createDtlnDenoise, createFastenhancerStream };

/**
 * Inline AudioWorklet denoise node (dtln / native passthrough).
 * fastenhancer uses createFastenhancerStream() instead — different integration path.
 * @param {AudioContext} audioContext
 * @param {'off'|'native'|'dtln'|'fastenhancer'} mode
 * @param {{ bypassUntilReady?: boolean }} [options]
 */
export async function createDenoiseNode(audioContext, mode, options = {}) {
  if (mode === "dtln") {
    return createDtlnDenoise(audioContext, options);
  }
  return createNativeDenoise();
}
