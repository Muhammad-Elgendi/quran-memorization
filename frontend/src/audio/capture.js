/** @typedef {'off'|'native'|'dtln'|'fastenhancer'} DenoiseMode */

/** @returns {DenoiseMode} */
export function resolveDenoiseMode() {
  const raw = import.meta.env.VITE_AUDIO_DENOISE ?? "dtln";
  if (raw === "off" || raw === "native" || raw === "dtln" || raw === "fastenhancer") {
    return raw;
  }
  return "dtln";
}

/** @returns {'native'|'off'} */
export function resolveFallbackMode() {
  const raw = import.meta.env.VITE_AUDIO_DENOISE_FALLBACK ?? "native";
  return raw === "off" ? "off" : "native";
}

export function bypassUntilReady() {
  return import.meta.env.VITE_AUDIO_BYPASS_UNTIL_READY !== "false";
}

/** @param {DenoiseMode} mode */
export function usesNeuralDenoise(mode) {
  return mode === "dtln" || mode === "fastenhancer";
}

/**
 * Browser capture constraints tuned to avoid double-processing with neural denoise.
 * @param {DenoiseMode} mode
 * @returns {MediaTrackConstraints}
 */
export function getAudioConstraints(mode) {
  if (usesNeuralDenoise(mode)) {
    return {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: false,
      autoGainControl: false,
    };
  }
  return {
    channelCount: 1,
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  };
}

/** @param {DenoiseMode} mode @param {MediaTrackConstraints} constraints */
export function logAudioProfile(mode, constraints) {
  console.info("[audio] capture profile", { denoise: mode, constraints });
}

/** Preferred AudioContext sample rate for a denoise mode (undefined = device default). */
export function preferredSampleRate(mode) {
  if (mode === "dtln") {
    return 16000;
  }
  return undefined;
}
