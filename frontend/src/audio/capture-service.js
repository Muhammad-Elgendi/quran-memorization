import {
  getAudioConstraints,
  logAudioProfile,
  preferredSampleRate,
  resolveDenoiseMode,
  resolveFallbackMode,
  usesNeuralDenoise,
} from "./capture.js";
import { createDenoiseNode, createFastenhancerStream } from "./denoise/index.js";

const PCM_WORKLET_URL = "/pcm-worklet.js";

/**
 * @typedef {Object} AudioGraphHandle
 * @property {MediaStream} rawStream
 * @property {MediaStream|null} processedStream
 * @property {AudioContext} audioContext
 * @property {string} denoiseMode
 * @property {boolean} fallbackUsed
 * @property {() => Promise<void>} stop
 */

/**
 * @param {Object} [options]
 * @param {import('./capture.js').DenoiseMode} [options.denoise]
 * @param {number} [options.chunkMs]
 * @param {(buffer: ArrayBuffer) => void} [options.onPcmChunk]
 * @param {(err: unknown) => void} [options.onError]
 * @param {boolean} [options.includeProcessedOutput]
 * @param {boolean} [_retried]
 * @returns {Promise<AudioGraphHandle & { pcmNode?: AudioWorkletNode }>}
 */
export async function openAudioGraph(
  {
    denoise = resolveDenoiseMode(),
    chunkMs = 250,
    onPcmChunk,
    onError,
    includeProcessedOutput = false,
  } = {},
  _retried = false,
) {
  const constraints = getAudioConstraints(denoise);
  const rawStream = await navigator.mediaDevices.getUserMedia({ audio: constraints });
  logAudioProfile(denoise, constraints);

  let effectiveMode = denoise;
  let fallbackUsed = false;
  let denoiseHandle = null;
  let fastenhancerHandle = null;
  /** @type {MediaStream} */
  let captureStream = rawStream;

  const rate = preferredSampleRate(effectiveMode);
  let audioContext;
  try {
    audioContext = rate
      ? new AudioContext({ sampleRate: rate })
      : new AudioContext();
  } catch {
    audioContext = new AudioContext();
  }
  await audioContext.resume();

  if (effectiveMode === "fastenhancer") {
    try {
      fastenhancerHandle = await createFastenhancerStream(rawStream);
      await fastenhancerHandle.ready;
      captureStream = fastenhancerHandle.outputStream;
    } catch (err) {
      console.warn("[audio] fastenhancer init failed:", err);
      if (!_retried) {
        await closeContext(audioContext, rawStream);
        fallbackUsed = true;
        const fallback = resolveFallbackMode();
        const result = await openAudioGraph(
          {
            denoise: fallback,
            chunkMs,
            onPcmChunk,
            onError,
            includeProcessedOutput,
          },
          true,
        );
        return { ...result, fallbackUsed: true };
      }
      throw err;
    }
  }

  const source = audioContext.createMediaStreamSource(captureStream);
  /** @type {AudioNode} */
  let tail = source;

  if (effectiveMode === "dtln") {
    try {
      denoiseHandle = await createDenoiseNode(audioContext, "dtln");
      if (denoiseHandle.node) {
        source.connect(denoiseHandle.node);
        tail = denoiseHandle.node;
      }
      // LiteRT may log WebGPU/NPU probe noise while loading; ready means wasm/CPU is live.
      await denoiseHandle.ready;
    } catch (err) {
      console.warn("[audio] DTLN init failed:", err);
      try {
        source.disconnect();
        denoiseHandle?.dispose();
      } catch {
        /* ignore */
      }
      if (!_retried) {
        await closeContext(audioContext, rawStream);
        fallbackUsed = true;
        const fallback = resolveFallbackMode();
        const result = await openAudioGraph(
          {
            denoise: fallback,
            chunkMs,
            onPcmChunk,
            onError,
            includeProcessedOutput,
          },
          true,
        );
        return { ...result, fallbackUsed: true };
      }
      throw err;
    }
  }

  /** @type {MediaStream|null} */
  let processedStream = null;
  /** @type {MediaStreamAudioDestinationNode|null} */
  let destination = null;

  if (includeProcessedOutput) {
    destination = audioContext.createMediaStreamDestination();
    tail.connect(destination);
    processedStream = destination.stream;
  }

  /** @type {AudioWorkletNode|undefined} */
  let pcmNode;
  if (onPcmChunk) {
    await audioContext.audioWorklet.addModule(PCM_WORKLET_URL);
    pcmNode = new AudioWorkletNode(audioContext, "pcm-capture-processor", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      channelCount: 1,
      processorOptions: {
        targetRate: 16000,
        chunkMs,
      },
    });

    pcmNode.port.onmessage = (event) => {
      if (event.data) {
        onPcmChunk(event.data);
      }
    };
    pcmNode.onprocessorerror = (err) => {
      onError?.(err);
    };

    tail.connect(pcmNode);
    const mute = audioContext.createGain();
    mute.gain.value = 0;
    pcmNode.connect(mute);
    mute.connect(audioContext.destination);
  } else if (!includeProcessedOutput) {
    const mute = audioContext.createGain();
    mute.gain.value = 0;
    tail.connect(mute);
    mute.connect(audioContext.destination);
  }

  return {
    rawStream,
    processedStream,
    audioContext,
    pcmNode,
    denoiseMode: effectiveMode,
    fallbackUsed,
    async stop() {
      try {
        if (pcmNode) {
          pcmNode.port.onmessage = null;
          pcmNode.disconnect();
        }
        source.disconnect();
        denoiseHandle?.node?.disconnect();
        destination?.disconnect();
        tail.disconnect();
      } catch {
        /* ignore */
      }
      denoiseHandle?.dispose();
      fastenhancerHandle?.dispose();
      rawStream.getTracks().forEach((t) => t.stop());
      await closeContext(audioContext, null);
    },
  };
}

async function closeContext(audioContext, stream) {
  stream?.getTracks().forEach((t) => t.stop());
  if (audioContext.state !== "closed") {
    await audioContext.close();
  }
}

/**
 * Continuous PCM capture for the WebSocket stream path.
 */
export async function startPcmCapture({ chunkMs = 250, onChunk, onError } = {}) {
  const graph = await openAudioGraph({
    chunkMs,
    onPcmChunk: onChunk,
    onError,
    includeProcessedOutput: false,
  });

  return {
    stream: graph.rawStream,
    audioContext: graph.audioContext,
    node: graph.pcmNode,
    denoiseMode: graph.denoiseMode,
    fallbackUsed: graph.fallbackUsed,
    stop: graph.stop,
  };
}

/**
 * Processed mic stream for REST MediaRecorder (denoised when enabled).
 */
export async function startProcessedStream() {
  const graph = await openAudioGraph({
    includeProcessedOutput: true,
  });

  if (!graph.processedStream) {
    await graph.stop();
    throw new Error("Processed stream unavailable");
  }

  return {
    stream: graph.processedStream,
    rawStream: graph.rawStream,
    denoiseMode: graph.denoiseMode,
    fallbackUsed: graph.fallbackUsed,
    stop: graph.stop,
  };
}

/** @param {import('./capture.js').DenoiseMode} mode */
export function describeDenoiseMode(mode) {
  switch (mode) {
    case "dtln":
      return "neural (DTLN)";
    case "fastenhancer":
      return "neural (FastEnhancer)";
    case "native":
      return "browser native";
    default:
      return "off";
  }
}

export { usesNeuralDenoise, resolveDenoiseMode, getAudioConstraints };
