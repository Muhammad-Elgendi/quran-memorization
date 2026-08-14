import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createAttemptWarner,
  playWarningTone,
  primeWarningAudio,
  resetWarningToneState,
  WARNING_TONE,
} from "../src/audio/warning-tone.js";

function makeMockContext({ state = "running" } = {}) {
  const oscillator = {
    type: "sine",
    connect: vi.fn(),
    disconnect: vi.fn(),
    frequency: { value: 0, setValueAtTime: vi.fn() },
    start: vi.fn(),
    stop: vi.fn(),
    onended: null,
  };
  const gain = {
    connect: vi.fn(),
    disconnect: vi.fn(),
    gain: { value: 0, setValueAtTime: vi.fn() },
  };
  const ctx = {
    state,
    currentTime: 1.5,
    destination: { id: "speakers" },
    resume: vi.fn(async () => {
      ctx.state = "running";
    }),
    createOscillator: vi.fn(() => oscillator),
    createGain: vi.fn(() => gain),
  };
  return { ctx, oscillator, gain };
}

describe("playWarningTone", () => {
  beforeEach(() => {
    resetWarningToneState();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetWarningToneState();
  });

  it("resumes a suspended shared context before starting the oscillator", async () => {
    const { ctx, oscillator, gain } = makeMockContext({ state: "suspended" });
    const Ctor = vi.fn(() => ctx);
    vi.stubGlobal("AudioContext", Ctor);

    await playWarningTone();

    expect(ctx.resume).toHaveBeenCalled();
    expect(oscillator.frequency.setValueAtTime).toHaveBeenCalledWith(
      WARNING_TONE.frequency,
      ctx.currentTime,
    );
    expect(gain.gain.setValueAtTime).toHaveBeenCalledWith(
      WARNING_TONE.gain,
      ctx.currentTime,
    );
    expect(oscillator.connect).toHaveBeenCalledWith(gain);
    expect(gain.connect).toHaveBeenCalledWith(ctx.destination);
    expect(oscillator.start).toHaveBeenCalledWith(ctx.currentTime);
    expect(oscillator.stop).toHaveBeenCalledWith(
      ctx.currentTime + WARNING_TONE.duration,
    );
  });

  it("falls back to the capture context when shared stays suspended", async () => {
    const shared = makeMockContext({ state: "suspended" });
    shared.ctx.resume = vi.fn(async () => {
      /* remain suspended (autoplay blocked) */
    });
    const capture = makeMockContext({ state: "running" });
    vi.stubGlobal(
      "AudioContext",
      vi.fn(() => shared.ctx),
    );

    await playWarningTone({ audioContext: capture.ctx });

    expect(capture.oscillator.start).toHaveBeenCalled();
    expect(shared.oscillator.start).not.toHaveBeenCalled();
  });

  it("prefers a primed shared context over a live capture context", async () => {
    const shared = makeMockContext({ state: "running" });
    const capture = makeMockContext({ state: "running" });
    vi.stubGlobal(
      "AudioContext",
      vi.fn(() => shared.ctx),
    );

    await primeWarningAudio();
    await playWarningTone({ audioContext: capture.ctx });

    expect(shared.oscillator.start).toHaveBeenCalled();
    expect(capture.oscillator.start).not.toHaveBeenCalled();
  });

  it("ignores a second play while the tone is still active", async () => {
    const { ctx, oscillator } = makeMockContext();
    vi.stubGlobal(
      "AudioContext",
      vi.fn(() => ctx),
    );

    await playWarningTone();
    await playWarningTone();

    expect(ctx.createOscillator).toHaveBeenCalledTimes(1);
    expect(oscillator.start).toHaveBeenCalledTimes(1);
  });

  it("clears the playing guard when the oscillator ends", async () => {
    const { ctx, oscillator } = makeMockContext();
    vi.stubGlobal(
      "AudioContext",
      vi.fn(() => ctx),
    );

    await playWarningTone();
    oscillator.onended();
    await playWarningTone();

    expect(ctx.createOscillator).toHaveBeenCalledTimes(2);
  });
});

describe("createAttemptWarner", () => {
  beforeEach(() => {
    resetWarningToneState();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetWarningToneState();
  });

  it("plays once per attempt and no-ops a duplicate fail result", async () => {
    const { ctx } = makeMockContext();
    vi.stubGlobal(
      "AudioContext",
      vi.fn(() => ctx),
    );
    const warner = createAttemptWarner();
    const fail = {
      surah: 1,
      ayah: 2,
      attempt: 1,
      passed: false,
      warning: true,
    };

    expect(warner.maybeWarn(fail)).toBe(true);
    expect(warner.maybeWarn(fail)).toBe(false);
    await vi.waitFor(() => expect(ctx.createOscillator).toHaveBeenCalledTimes(1));
  });

  it("does not warn on a pass", () => {
    const { ctx } = makeMockContext();
    vi.stubGlobal(
      "AudioContext",
      vi.fn(() => ctx),
    );
    const warner = createAttemptWarner();
    expect(
      warner.maybeWarn({
        surah: 1,
        ayah: 2,
        attempt: 1,
        passed: true,
        warning: false,
      }),
    ).toBe(false);
    expect(ctx.createOscillator).not.toHaveBeenCalled();
  });

  it("warns again after reset (new ayah / session)", async () => {
    const { ctx, oscillator } = makeMockContext();
    vi.stubGlobal(
      "AudioContext",
      vi.fn(() => ctx),
    );
    const warner = createAttemptWarner();
    const fail = {
      surah: 1,
      ayah: 2,
      attempt: 1,
      passed: false,
      warning: true,
    };
    warner.maybeWarn(fail);
    await vi.waitFor(() => expect(oscillator.start).toHaveBeenCalled());
    oscillator.onended();
    ctx.createOscillator.mockClear();
    warner.reset();
    expect(warner.maybeWarn(fail)).toBe(true);
    await vi.waitFor(() => expect(ctx.createOscillator).toHaveBeenCalledTimes(1));
  });
});
