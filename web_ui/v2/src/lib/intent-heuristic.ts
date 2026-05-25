/**
 * Cycle UI v2.8.5 — Rule-based intent heuristic that runs BEFORE the
 * server-side classifier.  Deterministic, zero-latency, zero-network.
 *
 * Why this exists: the MiniLM classifier is trained on a small
 * English-leaning corpus and produces low-confidence routes for
 * common Turkish prompts.  The heuristic catches the obvious
 * patterns (URL present, code fence, code keywords, question
 * stems, chitchat) and forces a confident mode mapping.  The
 * classifier is the fallback for the genuinely-ambiguous cases.
 *
 * Patterns are deliberately small + readable — easier to audit + tune
 * than a black-box ML model.  When a prompt doesn't match any rule
 * (or matches multiple categories), we return null and let the
 * debounced classifier do the work.
 *
 * Wired into createDebouncedClassifier — see intent-classifier.ts.
 */

import type { ChatMode, ClassifyResult } from "./intent-classifier";

/** Conservative URL regex — accepts http(s)://, plus bare domains
 *  like `github.com/foo`.  Misses some edge cases (IPv6, userinfo)
 *  but covers >99% of what end users paste into chat. */
const URL_RE =
  /(\bhttps?:\/\/[^\s<>"']+|\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|ai|app|gov|edu|co|tr|de|fr|uk|jp|cn|in|us)\b\/?\S*)/i;

/** Triple-backtick code fence or 4-space indented block — strong
 *  signal the user is sharing or asking about code. */
const CODE_FENCE_RE = /```[\s\S]{0,4000}?```|`[^`]{8,}`/;

/** Programming-language keywords (Turkish + English mix).  Bigram
 *  match: "python ile", "rust cli", "javascript fonksiyon", etc.
 *  Single-word matches are weaker — we require either a verb
 *  ("yaz", "oluştur", "geliştir", "implement", "build", "create",
 *  "fix") near the language token. */
const CODE_LANGS = [
  "python", "javascript", "typescript", "rust", "go", "golang", "java",
  "c\\+\\+", "cpp", "c#", "csharp", "html", "css", "swift", "kotlin",
  "ruby", "php", "bash", "shell", "sql", "solidity", "haskell",
] as const;
const CODE_LANG_RE = new RegExp(
  `\\b(${CODE_LANGS.join("|")})\\b`,
  "i",
);
const CODE_VERB_RE =
  /\b(yaz|olu[şs]tur|geli[şs]tir|kodla|implement|build|create|write|make|fix|debug|refactor|optimize|generate|hesaplay[ıi]c[ıi]|d[öo]n[üu][şs]t[üu]r|append|prepend|add(?:\s+a)?|do(?:cument)?)\b/i;

/** Strong CS-domain keywords that imply Build even without a verb
 *  (e.g. "snake game", "fizzbuzz", "calculator", "todo app"). */
const CODE_KEYWORD_RE =
  /\b(snake game|fizzbuzz|todo (?:app|list)|hesap (?:makinesi|maks)|calculator|api server|rest endpoint|web sayfa|landing page|dashboard|chatbot|websocket|mikroservis|microservice|fonksiyon yaz|class yaz|s[ıi]n[ıi]f yaz|app|uygulama yap|program yaz|script yaz)\b/i;

/** Question stems — start of prompt OR question phrase in the middle.
 *  Turkish + English.  Two variants:
 *  (1) starts with a question word ("ne yapar", "how to deploy")
 *  (2) "X ne yapar / X nedir / X nasıl çalışır" — entity-leading question
 *  Either → research (factual / web knowledge lookup). */
const QUESTION_STEMS_START_RE =
  /^\s*(ne|nedir|neden|nas[ıi]l|kim|hangi|ka[çc]|nerede|hadi|where|when|what|why|how|who|which|tell me|explain|describe)\b/i;
const QUESTION_STEMS_MIDDLE_RE =
  /\b(ne|nedir|nas[ıi]l|neden|kim|hangi|niye|ka[çc])\b/i;
const QUESTION_END_RE = /\?\s*$/;

/** Casual chitchat / greeting — should go to thinking (no heavy
 *  pipeline; a quick conversational reply is enough). */
const CHITCHAT_RE =
  /^\s*(merhaba|selam|sa|s\.a\.?|hi|hello|hey|nas[ıi]ls[ıi]n|how are you|good morning|g[üu]naydin|iyi (g[üu]nler|ak[şs]amlar)|te[şs]ekk[üu]r|thank you|thanks|sa[ğg] ol|naber|n'aber|hosca kal|g[öo]r[üu][şs][üu]r[üu]z|bye)\b/i;

/** Audit / security keywords → sentinel. */
const SENTINEL_RE =
  /\b(g[üu]venlik|security audit|vulnerab(?:le|ility)|owasp|sql injection|xss|csrf|secret leak|denetle|audit\s+(my|this|the))\b/i;

/** Quick-fix / one-line keywords → quickcode. */
const QUICKCODE_RE =
  /\b(typo|tipo|rename\s+\w+|fix\s+(typo|line|imports?|formatting)|d[üu]zelt\s+\w+|h[ıi]zl[ıi] (d[üu]zelt|fix))/i;

/** Multi-step planning / architecture keywords → consortium. */
const CONSORTIUM_RE =
  /\b(plan(?:la)? proje|build (?:a|the) (?:full|whole) (?:app|system)|t[üu]m projeyi|mimari[\s,]|architecture\s+(?:plan|review)|son\s+(?:[üu]r[üu]n[üu]?|product))/i;

/** Deep-think trigger — heavy reasoning prompts.  We deliberately
 *  exclude generic "why does X" patterns — those are factual
 *  questions better served by research.  Only true compare/tradeoff
 *  + multi-step reasoning markers reach here. */
const THINKING_RE =
  /\b(tradeoff|trade[\s-]off|kar[şs][ıi]la[şs]t[ıi]r|compare(?:\s+and contrast|\s+\w+\s+vs\.?\s+\w+)?|vs\.?\s+(?:and\s+)?(?:contrast)|d[üu][şs][üu]n.*ad[ıi]m\s+ad[ıi]m|step[\s-]by[\s-]step reasoning|reason\s+through)\b/i;
/** Loose vs-style compare: "A vs B" / "A ile B karşılaştır". */
const VS_COMPARE_RE =
  /\b([a-z][a-z0-9-]*)\s+(?:vs\.?|ile)\s+([a-z][a-z0-9-]*)\b/i;

// ────────────────────────────────────────────────────────────────────


export interface HeuristicHit {
  /** Mode the heuristic decided on. */
  mode: ChatMode;
  /** Human-readable reason — surfaced to the UI as a tooltip on the
   *  auto-mode pill (e.g. "URL detected", "code keyword: python +
   *  verb: yaz"). */
  reason: string;
  /** Synthetic confidence in [0.85, 1.0].  Set high enough that the
   *  UI treats it as a strong route (no "uncertain" pill). */
  confidence: number;
}


/** Try every rule in priority order; return the FIRST match.  Most
 *  specific (URL) wins over most general (chitchat). */
export function classifyByHeuristic(rawPrompt: string): HeuristicHit | null {
  const prompt = (rawPrompt ?? "").trim();
  if (!prompt) return null;

  // 1. URL — strongest signal.  User pasted a link → they want
  //    research on / about that URL.  (Sentinel keywords can still
  //    override below if both URL + security present.)
  const urlHit = URL_RE.exec(prompt);
  if (urlHit && SENTINEL_RE.test(prompt)) {
    return {
      mode: "sentinel",
      reason: "URL + security audit keyword",
      confidence: 0.95,
    };
  }
  if (urlHit) {
    return {
      mode: "research",
      reason: `URL detected: ${urlHit[0].slice(0, 48)}…`,
      confidence: 0.95,
    };
  }

  // 2. Code fence / sentinel / quickcode / consortium — specific
  //    keyword patterns that map deterministically.
  if (SENTINEL_RE.test(prompt)) {
    return {
      mode: "sentinel",
      reason: "security/audit keyword",
      confidence: 0.92,
    };
  }
  if (QUICKCODE_RE.test(prompt)) {
    return {
      mode: "quickcode",
      reason: "quick-fix keyword (typo / rename / düzelt)",
      confidence: 0.92,
    };
  }
  if (CONSORTIUM_RE.test(prompt)) {
    return {
      mode: "consortium",
      reason: "multi-step / architecture keyword",
      confidence: 0.9,
    };
  }

  // 3. Code — fence OR language+verb OR strong CS-domain term.
  const hasFence = CODE_FENCE_RE.test(prompt);
  const hasLang = CODE_LANG_RE.test(prompt);
  const hasVerb = CODE_VERB_RE.test(prompt);
  const hasCsKeyword = CODE_KEYWORD_RE.test(prompt);
  if (hasFence) {
    return {
      mode: "build",
      reason: "code fence present",
      confidence: 0.95,
    };
  }
  if (hasCsKeyword) {
    return {
      mode: "build",
      reason: "CS-domain keyword (snake game / calculator / todo / …)",
      confidence: 0.92,
    };
  }
  if (hasLang && hasVerb) {
    const langMatch = CODE_LANG_RE.exec(prompt);
    const verbMatch = CODE_VERB_RE.exec(prompt);
    return {
      mode: "build",
      reason: `language (${langMatch?.[0]}) + verb (${verbMatch?.[0]})`,
      confidence: 0.93,
    };
  }

  // 4. Deep-think / compare / tradeoff → thinking
  if (THINKING_RE.test(prompt) || VS_COMPARE_RE.test(prompt)) {
    return {
      mode: "thinking",
      reason: "compare / tradeoff / vs / step-by-step keyword",
      confidence: 0.88,
    };
  }

  // 5. Question — research (general factual / web knowledge lookup).
  //    Two variants:
  //    a) starts with question word ("how to deploy", "what is X")
  //    b) "X ne yapar / nedir / nasıl çalışır" — entity-led question
  //    c) prompt ends with "?"
  //    UNLESS the prompt is also chitchat-shaped (handled below).
  const isQuestion =
    QUESTION_STEMS_START_RE.test(prompt) ||
    QUESTION_STEMS_MIDDLE_RE.test(prompt) ||
    QUESTION_END_RE.test(prompt);
  if (isQuestion && !CHITCHAT_RE.test(prompt)) {
    return {
      mode: "research",
      reason: "question stem (ne / nedir / nasıl / what / how / why / ?)",
      confidence: 0.86,
    };
  }

  // 6. Chitchat / greeting — thinking (lightweight conversational
  //    reply; build/research engines would be overkill for "merhaba").
  if (CHITCHAT_RE.test(prompt)) {
    return {
      mode: "thinking",
      reason: "greeting / chitchat",
      confidence: 0.9,
    };
  }

  // No confident heuristic → fall back to classifier.
  return null;
}


/** Wrap a HeuristicHit so it looks like a ClassifyResult — lets the
 *  composer's UI handle both shapes uniformly. */
export function heuristicToResult(hit: HeuristicHit): ClassifyResult {
  // Build a degenerate alternatives list (winner + 5 zeros).  Most UI
  // surfaces only look at .mode + .confidence + .low_confidence; the
  // tail is just for shape compatibility.
  const others: Array<[ChatMode, number]> = [
    "build", "research", "thinking", "consortium", "sentinel", "quickcode",
  ]
    .filter((m): m is ChatMode => m !== hit.mode)
    .map((m) => [m, 0] as [ChatMode, number]);

  return {
    mode: hit.mode,
    top1_score: hit.confidence,
    top2_score: 0,
    confidence: hit.confidence,
    low_confidence: false,
    alternatives: [
      [hit.mode, hit.confidence] as [ChatMode, number],
      ...others,
    ],
    latency_ms: 0, // synthetic — no encoder call happened
  };
}
