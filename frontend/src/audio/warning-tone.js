/** Phase 1 / Continuous fail cue: 660 Hz, ~0.3 s, modest gain. */

export const WARNING_TONE = {
  frequency: 660,
  duration: 0.3,
  gain: 0.35,
};

let sharedContext = null;
let tonePlaying = false;

function audioContextCtor() {
  return globalThis.AudioContext || globalThis.webkitAudioContext || null;
}

function contextUsable(ctx) {
  return Boolean(
    ctx &&
      typeof ctx.createOscillator === "function" &&
      ctx.state !== "closed",
  );
}

function getSharedContext() {
  const Ctor = audioContextCtor();
  if (!Ctor) {
    return null;
  }
  if (!contextUsable(sharedContext)) {
    sharedContext = new Ctor();
  }
  return sharedContext;
}

/**
 * Unlock / resume the dedicated playback context during a user gesture
 * (Start Recording / Start Recitation). Capture graphs are often muted or
 * closed by the time a fail result arrives — do not rely on them alone.
 */
export async function primeWarningAudio() {
  const ctx = getSharedContext();
  if (!ctx) {
    return null;
  }
  if (ctx.state === "suspended" && typeof ctx.resume === "function") {
    try {
      await ctx.resume();
    } catch {
      /* autoplay may still block */
    }
  }
  return ctx;
}

async function resumeIfNeeded(ctx) {
  if (ctx && ctx.state === "suspended" && typeof ctx.resume === "function") {
    try {
      await ctx.resume();
    } catch {
      /* autoplay may still block */
    }
  }
  return ctx;
}

/**
 * Play the warning oscillator on speakers.
 * Prefer a gesture-primed shared playback context (default sample rate). Fall
 * back to a live capture AudioContext if shared is still suspended. Never
 * connect into the PCM processor graph — only `audioContext.destination`.
 *
 * @param {{ audioContext?: AudioContext | null }} [options]
 */
export async function playWarningTone({ audioContext } = {}) {
  if (tonePlaying) {
    return;
  }
  let ctx = await primeWarningAudio();
  if (!contextUsable(ctx) || ctx.state !== "running") {
    if (contextUsable(audioContext)) {
      ctx = await resumeIfNeeded(audioContext);
    }
  }
  if (!contextUsable(ctx)) {
    return;
  }
  await resumeIfNeeded(ctx);
  if (tonePlaying) {
    return;
  }
  tonePlaying = true;
  try {
    const oscillator = ctx.createOscillator();
    const gain = ctx.createGain();
    oscillator.type = "sine";
    oscillator.connect(gain);
    gain.connect(ctx.destination);
    const t0 = ctx.currentTime || 0;
    oscillator.frequency.setValueAtTime(WARNING_TONE.frequency, t0);
    gain.gain.setValueAtTime(WARNING_TONE.gain, t0);
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
    oscillator.start(t0);
    oscillator.stop(t0 + WARNING_TONE.duration);
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
