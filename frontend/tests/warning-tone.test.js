import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createAttemptWarner,
  playWarningTone,
  resetWarningToneState,
  WARNING_TONE,
} from "../src/audio/warning-tone.js";

function makeMockContext({ state = "running" } = {}) {
  const oscillator = {
    connect: vi.fn(),
    disconnect: vi.fn(),
    frequency: { value: 0 },
    start: vi.fn(),
    stop: vi.fn(),
    onended: null,
  };
  const gain = {
    connect: vi.fn(),
    disconnect: vi.fn(),
    gain: { value: 0 },
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

  it("resumes a suspended context before starting the oscillator", async () => {
    const { ctx, oscillator, gain } = makeMockContext({ state: "suspended" });
    const Ctor = vi.fn();
    vi.stubGlobal("AudioContext", Ctor);

    await playWarningTone({ audioContext: ctx });

    expect(ctx.resume).toHaveBeenCalledTimes(1);
    expect(Ctor).not.toHaveBeenCalled();
    expect(oscillator.frequency.value).toBe(WARNING_TONE.frequency);
    expect(gain.gain.value).toBe(WARNING_TONE.gain);
    expect(oscillator.connect).toHaveBeenCalledWith(gain);
    expect(gain.connect).toHaveBeenCalledWith(ctx.destination);
    expect(oscillator.start).toHaveBeenCalled();
    expect(oscillator.stop).toHaveBeenCalledWith(ctx.currentTime + WARNING_TONE.duration);
  });

  it("prefers the provided capture context and does not construct another", async () => {
    const { ctx, oscillator } = makeMockContext();
    const Ctor = vi.fn(() => {
      throw new Error("must not construct a second AudioContext");
    });
    vi.stubGlobal("AudioContext", Ctor);

    await playWarningTone({ audioContext: ctx });

    expect(Ctor).not.toHaveBeenCalled();
    expect(oscillator.start).toHaveBeenCalled();
  });

  it("ignores a second play while the tone is still active", async () => {
    const { ctx, oscillator } = makeMockContext();

    await playWarningTone({ audioContext: ctx });
    await playWarningTone({ audioContext: ctx });

    expect(ctx.createOscillator).toHaveBeenCalledTimes(1);
    expect(oscillator.start).toHaveBeenCalledTimes(1);
  });

  it("clears the playing guard when the oscillator ends", async () => {
    const { ctx, oscillator } = makeMockContext();

    await playWarningTone({ audioContext: ctx });
    oscillator.onended();
    await playWarningTone({ audioContext: ctx });

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
    const warner = createAttemptWarner();
    const fail = {
      surah: 1,
      ayah: 2,
      attempt: 1,
      passed: false,
      warning: true,
    };

    expect(warner.maybeWarn(fail, { audioContext: ctx })).toBe(true);
    expect(warner.maybeWarn(fail, { audioContext: ctx })).toBe(false);
    expect(ctx.createOscillator).toHaveBeenCalledTimes(1);
  });

  it("does not warn on a pass", () => {
    const { ctx } = makeMockContext();
    const warner = createAttemptWarner();
    expect(
      warner.maybeWarn(
        { surah: 1, ayah: 2, attempt: 1, passed: true, warning: false },
        { audioContext: ctx },
      )
    ).toBe(false);
    expect(ctx.createOscillator).not.toHaveBeenCalled();
  });

  it("warns again after reset (new ayah / session)", async () => {
    const { ctx, oscillator } = makeMockContext();
    const warner = createAttemptWarner();
    const fail = {
      surah: 1,
      ayah: 2,
      attempt: 1,
      passed: false,
      warning: true,
    };
    warner.maybeWarn(fail, { audioContext: ctx });
    oscillator.onended();
    ctx.createOscillator.mockClear();
    warner.reset();
    expect(warner.maybeWarn(fail, { audioContext: ctx })).toBe(true);
    expect(ctx.createOscillator).toHaveBeenCalledTimes(1);
  });
});
