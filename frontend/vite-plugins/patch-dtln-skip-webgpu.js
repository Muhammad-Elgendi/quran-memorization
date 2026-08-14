import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);

/**
 * LiteRT's Environment.create() probes navigator.gpu by default.
 * AudioWorkletGlobalScope has no navigator, so that probe always throws
 * (then falls back to wasm/CPU). Skip the probe: DTLN already compiles with
 * accelerator: "wasm" in this package.
 */
const FROM = "liteRt.setDefaultEnvironment(await Environment.create());";
const TO =
  "liteRt.setDefaultEnvironment(await Environment.create({ webGpuDevice: null }));";

function resolveProcessorPath() {
  const pkgJson = require.resolve("@workadventure/noise-suppression/package.json");
  return path.join(path.dirname(pkgJson), "dist/assets/audio-worklet-processor.js");
}

export function patchDtlnSkipWebGpu() {
  return {
    name: "patch-dtln-skip-webgpu",
    buildStart() {
      let processorPath;
      try {
        processorPath = resolveProcessorPath();
      } catch {
        this.warn("DTLN WebGPU skip patch: package not installed");
        return;
      }

      if (!fs.existsSync(processorPath)) {
        this.warn(`DTLN WebGPU skip patch: missing ${processorPath}`);
        return;
      }

      const code = fs.readFileSync(processorPath, "utf8");
      if (code.includes(TO)) {
        return;
      }
      if (!code.includes(FROM)) {
        this.warn(
          "DTLN WebGPU skip patch: needle not found — @workadventure/noise-suppression may have changed",
        );
        return;
      }
      fs.writeFileSync(processorPath, code.replace(FROM, TO));
      this.info(
        "Patched DTLN AudioWorklet to skip WebGPU probe (wasm/CPU only in worklet)",
      );
    },
  };
}
