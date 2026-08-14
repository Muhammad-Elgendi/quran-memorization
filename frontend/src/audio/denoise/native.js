/**
 * Passthrough — no neural denoise node; relies on browser capture constraints.
 * @returns {Promise<{ node: null, ready: Promise<void>, dispose: () => void }>}
 */
export async function createNativeDenoise() {
  return {
    node: null,
    ready: Promise.resolve(),
    dispose() {},
  };
}
