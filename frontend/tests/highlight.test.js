import { describe, expect, it } from "vitest";
import { heardTextFromMessage, wordsFromAlignment } from "../src/highlight.js";

describe("wordsFromAlignment", () => {
  it("marks pending when no alignment", () => {
    const words = wordsFromAlignment("الحمد لله رب", null);
    expect(words).toEqual([
      { word: "الحمد", status: "pending" },
      { word: "لله", status: "pending" },
      { word: "رب", status: "pending" },
    ]);
  });

  it("maps equal / replace / delete and skips inserts", () => {
    const text = "الحمد لله رب العالمين";
    const alignment = [
      { op: "equal", expected: "الحمد", recognized: "الحمد" },
      { op: "equal", expected: "لله", recognized: "لله" },
      { op: "insert", expected: null, recognized: "extra" },
      { op: "replace", expected: "رب", recognized: "ربب" },
      { op: "delete", expected: "العالمين", recognized: null },
    ];
    expect(wordsFromAlignment(text, alignment)).toEqual([
      { word: "الحمد", status: "match" },
      { word: "لله", status: "match" },
      { word: "رب", status: "wrong" },
      { word: "العالمين", status: "missing" },
    ]);
  });

  it("keeps mismatches pending on provisional live partials", () => {
    const text = "الرحمن الرحيم";
    const alignment = [
      { op: "equal", expected: "الرحمن", recognized: "الرحمن" },
      { op: "replace", expected: "الرحيم", recognized: "يضحين" },
    ];
    expect(wordsFromAlignment(text, alignment, { provisional: true })).toEqual([
      { word: "الرحمن", status: "match" },
      { word: "الرحيم", status: "pending" },
    ]);
  });
});

describe("heardTextFromMessage", () => {
  it("joins kept words only", () => {
    expect(
      heardTextFromMessage({
        recognized: "يا كلب نستعين",
        words: [
          { text: "يا", confidence: 0.2, kept: false },
          { text: "كلب", confidence: 0.1, kept: false },
          { text: "نستعين", confidence: 0.91, kept: true },
        ],
      })
    ).toBe("نستعين");
  });

  it("falls back to recognized and never uses raw_text", () => {
    expect(
      heardTextFromMessage({
        recognized: "نستعين",
        raw_text: "يا كلب نستعين",
        raw_recognized: "يا كلب نستعين",
      })
    ).toBe("نستعين");
  });
});
