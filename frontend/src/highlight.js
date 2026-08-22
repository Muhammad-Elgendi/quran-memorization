/**
 * Map SequenceMatcher-style alignment ops onto display words for highlighting.
 * @param {string} text
 * @param {Array<{op: string}>|null|undefined} alignment
 * @param {{provisional?: boolean}} [options] live Continuous partials: mismatch/missing
 *   stay pending until `ayah.result` (do not lock red on an incomplete window).
 * @returns {Array<{word: string, status: 'pending'|'match'|'wrong'|'missing'}>}
 */
export function wordsFromAlignment(text, alignment, options = {}) {
  const words = (text || "").trim().split(/\s+/).filter(Boolean);
  if (!words.length) {
    return [];
  }
  const statuses = words.map(() => "pending");
  if (!alignment?.length) {
    return words.map((word) => ({ word, status: "pending" }));
  }
  const provisional = Boolean(options.provisional);
  let ei = 0;
  for (const op of alignment) {
    if (op.op === "insert") {
      continue;
    }
    if (ei >= words.length) {
      break;
    }
    if (op.op === "equal") {
      statuses[ei] = "match";
    } else if (provisional) {
      statuses[ei] = "pending";
    } else if (op.op === "replace") {
      statuses[ei] = "wrong";
    } else if (op.op === "delete") {
      statuses[ei] = "missing";
    }
    ei += 1;
  }
  return words.map((word, i) => ({ word, status: statuses[i] }));
}

const WHISPER_ANGLE_TOKEN = /<\|[^|>]*\|>/g;

/**
 * Strip Whisper control / timestamp tokens that must never reach Heard.
 * @param {string} text
 * @returns {string}
 */
export function stripDecoderSpecialTokens(text) {
  const cleaned = String(text || "").replace(WHISPER_ANGLE_TOKEN, " ");
  return cleaned.split(/\s+/).filter(Boolean).join(" ");
}

/**
 * Heard line from a stream/REST payload: kept decoder words only, else `recognized`.
 * Never uses `raw_text` / `raw_recognized`.
 * @param {{recognized?: string, words?: Array<{text?: string, kept?: boolean}>}|null|undefined} msg
 * @returns {string}
 */
export function heardTextFromMessage(msg) {
  if (!msg) {
    return "";
  }
  if (Array.isArray(msg.words) && msg.words.length) {
    const kept = msg.words.filter((word) => word && word.kept === true && word.text);
    if (kept.length) {
      const joined = kept
        .map((word) => stripDecoderSpecialTokens(word.text))
        .filter(Boolean)
        .join(" ");
      return stripDecoderSpecialTokens(joined);
    }
  }
  return stripDecoderSpecialTokens(msg.recognized || "");
}
