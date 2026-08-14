/**
 * FastEnhancer stream denoiser (48 kHz native).
 * Returns a processed MediaStream — wire via createMediaStreamSource, not as an inline node.
 * @param {MediaStream} rawStream
 */
export async function createFastenhancerStream(rawStream) {
  const { loadModel } = await import("fastenhancer-web");

  const model = await loadModel("tiny");
  const denoiser = await model.createStreamDenoiser(rawStream);

  return {
    outputStream: denoiser.outputStream,
    ready: Promise.resolve(),
    dispose() {
      try {
        denoiser.destroy();
      } catch {
        /* ignore */
      }
    },
  };
}
