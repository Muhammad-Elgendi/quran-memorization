/** Phase 1 / Continuous fail cue: 660 Hz, ~0.3 s, modest gain. */

export const WARNING_TONE = {
  frequency: 660,
  duration: 0.3,
  gain: 0.2,
};

let sharedContext = null;
let tonePlaying = false;

function audioContextCtor() {
  return globalThis.AudioContext || globalThis.webkitAudioContext || null;
}

function getSharedContext() {
  const Ctor = audioContextCtor();
  if (!Ctor) {
    return null;
  }
  if (!sharedContext || sharedContext.state === "closed") {
    sharedContext = new Ctor();
  }
  return sharedContext;
}

/**
 * Play the warning oscillator on speakers.
 * Prefer a live capture AudioContext (mic already unlocked it). Never connect
 * into the PCM processor graph — only `audioContext.destination`.
 *
 * @param {{ audioContext?: AudioContext | null }} [options]
 */
export async function playWarningTone({ audioContext } = {}) {
  if (tonePlaying) {
    return;
  }
  const ctx = audioContext || getSharedContext();
  if (!ctx || typeof ctx.createOscillator !== "function") {
    return;
  }
  if (ctx.state === "suspended" && typeof ctx.resume === "function") {
    try {
      await ctx.resume();
    } catch {
      /* autoplay may still block; try to start anyway */
    }
  }
  if (tonePlaying) {
    return;
  }
  tonePlaying = true;
  try {
    const oscillator = ctx.createOscillator();
    const gain = ctx.createGain();
    oscillator.connect(gain);
    gain.connect(ctx.destination);
    oscillator.frequency.value = WARNING_TONE.frequency;
    gain.gain.value = WARNING_TONE.gain;
    const finish = () => {
      try {
        oscillator.disconnect();
        gain.disconnect();
      } catch {
        /* already disconnected */
      }
      tonePlaying = false;
    };
    oscillator.onended = finish;
    oscillator.start();
    oscillator.stop((ctx.currentTime || 0) + WARNING_TONE.duration);
  } catch {
    tonePlaying = false;
  }
}

export function warningAttemptKey(msg) {
  return `${msg.surah}:${msg.ayah}:${msg.attempt}`;
}

/** At most one warning tone per (surah, ayah, attempt). */
export function createAttemptWarner() {
  let warnedKey = null;
  return {
    reset() {
      warnedKey = null;
    },
    maybeWarn(msg, options) {
      if (!msg || (msg.passed && !msg.warning)) {
        return false;
      }
      const key = warningAttemptKey(msg);
      if (key === warnedKey) {
        return false;
      }
      warnedKey = key;
      void playWarningTone(options);
      return true;
    },
  };
}

/** Test helper: drop module-level oscillator / shared-context state. */
export function resetWarningToneState() {
  sharedContext = null;
  tonePlaying = false;
}
