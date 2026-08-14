import { describe, expect, it } from "vitest";
import {
  bypassUntilReady,
  getAudioConstraints,
  preferredSampleRate,
  resolveDenoiseMode,
  resolveFallbackMode,
  usesNeuralDenoise,
} from "../src/audio/capture.js";
import { createDenoiseNode } from "../src/audio/denoise/index.js";

describe("getAudioConstraints", () => {
  it("disables browser NS/AGC when neural denoise is active", () => {
    const c = getAudioConstraints("dtln");
    expect(c.noiseSuppression).toBe(false);
    expect(c.autoGainControl).toBe(false);
    expect(c.echoCancellation).toBe(true);
    expect(c.channelCount).toBe(1);
  });

  it("enables browser NS/AGC for native profile", () => {
    const c = getAudioConstraints("native");
    expect(c.noiseSuppression).toBe(true);
    expect(c.autoGainControl).toBe(true);
  });

  it("treats off like native constraints", () => {
    const c = getAudioConstraints("off");
    expect(c.noiseSuppression).toBe(true);
  });
});

describe("usesNeuralDenoise", () => {
  it("identifies neural modes", () => {
    expect(usesNeuralDenoise("dtln")).toBe(true);
    expect(usesNeuralDenoise("fastenhancer")).toBe(true);
    expect(usesNeuralDenoise("native")).toBe(false);
  });
});

describe("preferredSampleRate", () => {
  it("prefers 16 kHz for DTLN", () => {
    expect(preferredSampleRate("dtln")).toBe(16000);
    expect(preferredSampleRate("fastenhancer")).toBeUndefined();
  });
});

describe("resolveDenoiseMode", () => {
  it("defaults to off", () => {
    expect(resolveDenoiseMode()).toBe("off");
  });
});

describe("resolveFallbackMode", () => {
  it("defaults to native", () => {
    expect(resolveFallbackMode()).toBe("native");
  });
});

describe("bypassUntilReady", () => {
  it("defaults to true", () => {
    expect(bypassUntilReady()).toBe(true);
  });
});

describe("createDenoiseNode factory", () => {
  it("returns passthrough for native/off", async () => {
    const fakeCtx = {};
    const native = await createDenoiseNode(fakeCtx, "native");
    expect(native.node).toBeNull();
    await native.ready;

    const off = await createDenoiseNode(fakeCtx, "off");
    expect(off.node).toBeNull();
  });
});
