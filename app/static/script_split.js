"use strict";
/* Full-script import: split one pasted narration into per-scene scripts.

   Two modes:
   - labeled:    "SCENE 1", "Scene 01:", "[Scene 1]", "Scene 1 — Apartment" headings decide the scenes.
   - continuous: no labels; the text is divided into exactly N chronological parts using
                 paragraphs, sentences and an estimate of speaking time. This is an ESTIMATE —
                 the images are not analysed.

   Words are never rewritten, reordered, added or dropped: each part is a contiguous slice of the
   original text (only whitespace between pieces is normalised). Works in the browser and Node. */
const ScriptSplit = (() => {
  const ABBREVIATIONS = new Set(["mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "prof", "mt", "no",
    "approx", "e.g", "i.e", "a.m", "p.m", "u.s", "lt", "sgt", "capt", "gen"]);
  const HEADING = /^\s*(?:#{1,6}\s*)?[*_]*\s*[\[(]?\s*scene\s*#?\s*(\d{1,3})\s*[\])]?\s*[*_]*\s*(?:(?:[:.\-–—|]|(?=\s*[\[(]))\s*(.*?))?\s*[*_]*\s*$/i;
  const IMAGE_LINE = /^\s*(?:image|img|picture|file)\s*:\s*.+$/i;
  const LABEL_LINE = /^\s*(?:narration|script|voice\s?over|vo)\s*:\s*/i;
  // Boundary costs: lower = more natural place to cut between two scenes.
  const PENALTY = { paragraph: 0, sentence: 0.12, clause: 0.9, word: 2.5 };

  const words = (t) => (t || "").split(/\s+/).filter(Boolean);
  const squash = (t) => (t || "").replace(/[ \t]+/g, " ").replace(/ *\n */g, "\n").trim();

  function stripWrappingQuotes(text) {
    const t = text.trim();
    const m = t.match(/^["“]([\s\S]*)["”]$/);
    return m && !/["“”]/.test(m[1]) ? m[1].trim() : t;
  }

  /* ---------- labeled scripts ---------- */
  function parseHeading(line) {
    const m = line.match(HEADING);
    if (!m) return null;
    let rest = (m[2] || "").replace(/^[\[(]\s*|\s*[\])]$/g, "").trim();
    // "Scene 1 — Apartment" is a title; "Scene 1: At 8:43 PM, the world..." starts the narration.
    const isNarration = rest && (words(rest).length > 6 || /[.!?…"”]$/.test(rest));
    return { number: +m[1], title: isNarration ? "" : rest, inline: isNarration ? rest : "" };
  }

  function parseLabeled(text) {
    const lines = text.replace(/\r/g, "").split("\n");
    const sections = [];
    const preamble = [];
    let cur = null;
    for (const line of lines) {
      const h = parseHeading(line);
      if (h) {
        cur = { number: h.number, title: h.title, lines: h.inline ? [h.inline] : [] };
        sections.push(cur);
        continue;
      }
      if (!cur) { preamble.push(line); continue; }
      if (IMAGE_LINE.test(line)) continue;           // "Image: scene_01.png" is not narration
      cur.lines.push(line.replace(LABEL_LINE, ""));  // "Narration: ..." keeps only the text
    }
    for (const s of sections) {
      s.text = stripWrappingQuotes(squash(s.lines.join("\n")).replace(/\n{3,}/g, "\n\n"));
      delete s.lines;
    }
    return { sections, preamble: squash(preamble.join("\n")) };
  }

  /* ---------- continuous scripts ---------- */
  function splitSentences(paragraph) {
    const out = [];
    let start = 0, current = "";
    const re = /[.!?…]+["'”’)\]]*\s+/g;
    let m;
    const pieces = [];
    while ((m = re.exec(paragraph))) { pieces.push(paragraph.slice(start, m.index + m[0].length).trim()); start = m.index + m[0].length; }
    pieces.push(paragraph.slice(start).trim());
    for (const p of pieces.filter(Boolean)) {
      current = current ? `${current} ${p}` : p;
      const last = current.split(/\s+/).pop().replace(/\.+$/, "").toLowerCase().replace(/^["'(“]+/, "");
      if (ABBREVIATIONS.has(last) || (last.length === 1 && /[a-z]/.test(last))) continue;
      out.push(current); current = "";
    }
    if (current) out.push(current);
    return out;
  }

  function splitClauses(sentence) {
    const parts = sentence.split(/(?<=[,;:—–])\s+/);
    return parts.filter(Boolean);
  }

  function syllables(word) {
    const w = word.toLowerCase().replace(/[^a-z0-9]/g, "");
    if (!w) return 0;
    if (/^\d+$/.test(w)) return w.length * 1.5;          // numbers are spoken as several words
    const groups = w.replace(/e$/, "").match(/[aeiouy]+/g);
    return Math.max(1, groups ? groups.length : 1);
  }

  /* Estimated speaking time in seconds (only used to balance scenes against each other). */
  function estimateSeconds(text) {
    const ws = words(text);
    let s = ws.reduce((a, w) => a + syllables(w), 0) * 0.21;
    s += ((text.match(/[.!?…]+(\s|$)/g) || []).length) * 0.35;
    s += ((text.match(/[,;:—–]/g) || []).length) * 0.15;
    return s;
  }

  /* Units = smallest pieces we may cut between, each tagged with the kind of break after it. */
  function buildUnits(text, level) {
    const units = [];
    const paragraphs = text.split(/\n\s*\n/).map((p) => p.replace(/\s+/g, " ").trim()).filter(Boolean);
    paragraphs.forEach((para) => {
      const sentences = splitSentences(para);
      sentences.forEach((sentence, si) => {
        let pieces = [sentence];
        if (level >= 1) pieces = splitClauses(sentence);
        if (level >= 2) pieces = pieces.flatMap((c) => words(c));
        pieces.forEach((piece, pi) => {
          const lastInSentence = pi === pieces.length - 1;
          let brk = level >= 2 && !/[,;:—–]$/.test(piece) ? "word" : "clause";
          if (lastInSentence) brk = si === sentences.length - 1 ? "paragraph" : "sentence";
          units.push({ text: piece, brk, secs: estimateSeconds(piece) });
        });
      });
    });
    return units;
  }

  /* Split units into k contiguous groups: balance estimated duration, prefer natural breaks. */
  function bestSplit(units, k) {
    const n = units.length;
    const prefix = [0];
    units.forEach((u) => prefix.push(prefix[prefix.length - 1] + u.secs));
    const target = prefix[n] / k || 1;
    const cost = (i, j) => { const d = (prefix[j] - prefix[i] - target) / target; return d * d; };
    const INF = 1e18;
    const dp = Array.from({ length: k + 1 }, () => new Float64Array(n + 1).fill(INF));
    const from = Array.from({ length: k + 1 }, () => new Int32Array(n + 1).fill(-1));
    dp[0][0] = 0;
    for (let g = 1; g <= k; g++) {
      for (let j = g; j <= n - (k - g); j++) {
        for (let i = g - 1; i < j; i++) {
          if (dp[g - 1][i] >= INF) continue;
          const pen = i > 0 ? PENALTY[units[i - 1].brk] : 0;
          const v = dp[g - 1][i] + pen + cost(i, j);
          if (v < dp[g][j]) { dp[g][j] = v; from[g][j] = i; }
        }
      }
    }
    const cuts = [];
    for (let g = k, j = n; g > 0; g--) { const i = from[g][j]; cuts.unshift(i); j = i; }
    return cuts.concat(n);   // cuts[g] = first unit of group g; last entry = n
  }

  function joinUnits(units) {
    let out = "";
    units.forEach((u, idx) => {
      out += u.text;
      if (idx < units.length - 1) out += u.brk === "paragraph" ? "\n\n" : " ";
    });
    return out;
  }

  function distributeContinuous(text, k) {
    let units = [];
    let level = 0;
    for (; level <= 2; level++) {
      units = buildUnits(text, level);
      if (units.length >= k) break;
    }
    if (units.length < k) {    // fewer words than scenes: give every word its own scene
      const parts = units.map((u) => u.text);
      while (parts.length < k) parts.push("");
      return { parts, boundaries: parts.slice(1).map(() => "word"), level };
    }
    const cuts = bestSplit(units, k);
    const parts = [], boundaries = [];
    for (let g = 0; g < k; g++) {
      parts.push(joinUnits(units.slice(cuts[g], cuts[g + 1])));
      if (g > 0) boundaries.push(units[cuts[g] - 1].brk);
    }
    return { parts, boundaries, level };
  }

  /* ---------- public entry point ---------- */
  function analyse(rawText) {
    const text = (rawText || "").replace(/\r/g, "");
    const labeled = parseLabeled(text);
    const isLabeled = labeled.sections.length > 0;
    const body = isLabeled ? labeled.sections.map((s) => s.text).join("\n\n") : stripWrappingQuotes(text);
    const paragraphs = body.split(/\n\s*\n/).filter((p) => p.trim()).length;
    return { labeled: isLabeled, sections: labeled.sections, preamble: labeled.preamble,
      words: words(body).length, paragraphs, body };
  }

  /* Returns {mode, parts[], notes[], warnings[], boundaries[], titles[], extra}.
     parts.length === sceneCount (labeled mode may return more: extra sections become new scenes). */
  function assign(rawText, sceneCount) {
    const a = analyse(rawText);
    const warnings = [];
    if (a.labeled) {
      const nums = a.sections.map((s) => s.number);
      // Labels always decide the scene — never fall back to filling from scene 1, which would
      // overwrite scenes the script doesn't mention. A wrong label is named so it can be fixed.
      const repeated = [...new Set(nums.filter((v, i) => nums.indexOf(v) !== i))];
      const zero = nums.includes(0);
      if (repeated.length || zero) {
        const errors = repeated.map((v) => `SCENE ${v} appears ${nums.filter((x) => x === v).length} times: ` +
          a.sections.filter((s) => s.number === v).map((s) => `“${s.text.slice(0, 45)}…”`).join(" and ") + ".");
        if (zero) errors.push("SCENE 0 is not a scene number — scenes start at 1.");
        return { mode: "labeled", parts: [], titles: [], boundaries: [], analysis: a,
          warnings: [...errors, "Fix the label in your script, then click Auto assign again. Nothing was changed."] };
      }
      const outOfOrder = a.sections.filter((s, i) => i > 0 && s.number < a.sections[i - 1].number)
        .map((s) => `SCENE ${s.number}`);
      if (outOfOrder.length) warnings.push(`Out of order in your script: ${outOfOrder.join(", ")} (comes after a higher number). ` +
        "Each section still goes to the scene its label says — check that the label is right.");
      // Only labeled scenes change. Unlabeled ones are null = "keep as it is", so a script with
      // SCENE 11 and SCENE 12 simply adds those two scenes and leaves scenes 1–10 alone.
      const size = Math.max(sceneCount, ...nums);
      const parts = Array(size).fill(null), titles = Array(size).fill("");
      a.sections.forEach((s) => { parts[s.number - 1] = s.text; titles[s.number - 1] = s.title; });
      for (let i = sceneCount; i < size; i++) if (parts[i] === null) parts[i] = "";   // a gap in new scenes
      const gaps = []; for (let i = sceneCount; i < size; i++) if (!nums.includes(i + 1)) gaps.push(i + 1);
      if (gaps.length) warnings.push(`No section for new scene${gaps.length === 1 ? "" : "s"} ${gaps.join(", ")} — ${gaps.length === 1 ? "it" : "they"} will be added empty.`);
      const added = parts.length - sceneCount;
      if (added > 0) warnings.push(`${added} new scene${added === 1 ? "" : "s"} (${sceneCount + 1}${added > 1 ? `–${parts.length}` : ""}) will be added without an image — use 🎨 Generate missing to make ${added === 1 ? "its image" : "their images"}.`);
      if (a.preamble) warnings.push(`Text before the first scene label is not assigned: “${a.preamble.slice(0, 80)}${a.preamble.length > 80 ? "…" : ""}”`);
      return { mode: "labeled", parts, titles, boundaries: [], warnings, analysis: a };
    }
    if (sceneCount < 1) return { mode: "continuous", parts: [], titles: [], boundaries: [], warnings: ["Add your scene images first — the script is divided across them."], analysis: a };
    const d = distributeContinuous(a.body, sceneCount);
    if (d.level >= 1) warnings.push("There are fewer sentences than scenes, so some sentences had to be split at commas" + (d.level >= 2 ? " or between words." : "."));
    if (d.parts.some((p) => !p.trim())) warnings.push("The script is too short for this many scenes: some scenes are empty.");
    return { mode: "continuous", parts: d.parts, titles: d.parts.map(() => ""), boundaries: d.boundaries, warnings, analysis: a };
  }

  /* Pieces used when nudging a boundary in the review: sentences, else clauses, else words. */
  function pieces(text) {
    const t = (text || "").replace(/\s+/g, " ").trim();
    if (!t) return [];
    const s = splitSentences(t);
    if (s.length > 1) return s;
    const c = splitClauses(t);
    return c.length > 1 ? c : words(t);
  }

  /* Move one piece across the boundary between part i and i+1. dir -1: last piece of i goes down. */
  function moveBoundary(parts, i, dir) {
    const out = parts.slice();
    if (dir < 0) {
      const p = pieces(out[i]);
      if (!p.length) return out;
      const moved = p.pop();
      out[i] = p.join(" ");
      out[i + 1] = [moved, (out[i + 1] || "").trim()].filter(Boolean).join(" ");
    } else {
      const p = pieces(out[i + 1]);
      if (!p.length) return out;
      const moved = p.shift();
      out[i + 1] = p.join(" ");
      out[i] = [(out[i] || "").trim(), moved].filter(Boolean).join(" ");
    }
    return out;
  }

  return { analyse, assign, moveBoundary, estimateSeconds, words, splitSentences };
})();

if (typeof module !== "undefined") module.exports = ScriptSplit;
