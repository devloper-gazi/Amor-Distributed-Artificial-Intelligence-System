/**
 * Cycle UI v2.8.5 — intent-heuristic tests.
 *
 * The heuristic is a deterministic regex prepass; tests assert that
 * the obvious patterns (URL, code, question, chitchat, etc.) route
 * to the expected mode WITHOUT any network call.  Anything that
 * doesn't match a rule returns null so the server classifier can
 * take over.
 */

import { describe, it, expect } from "vitest";
import { classifyByHeuristic } from "./intent-heuristic";


describe("classifyByHeuristic — URL detection", () => {
  it("routes plain https URL to research", () => {
    const r = classifyByHeuristic("https://example.com");
    expect(r?.mode).toBe("research");
    expect(r?.reason).toMatch(/URL/);
  });

  it("routes URL with question to research", () => {
    const r = classifyByHeuristic(
      "https://github.com/anthropics/anthropic-sdk-python ne işe yarar",
    );
    expect(r?.mode).toBe("research");
  });

  it("routes bare domain (no scheme) to research", () => {
    const r = classifyByHeuristic("github.com/foo/bar açıklar mısın");
    expect(r?.mode).toBe("research");
  });

  it("routes URL + security keyword to sentinel", () => {
    const r = classifyByHeuristic(
      "https://example.com güvenlik denetimi yapar mısın",
    );
    expect(r?.mode).toBe("sentinel");
  });
});


describe("classifyByHeuristic — code detection", () => {
  it("routes language + verb (Turkish) to build", () => {
    const r = classifyByHeuristic("python ile fibonacci yaz");
    expect(r?.mode).toBe("build");
    expect(r?.reason).toMatch(/language|verb/i);
  });

  it("routes 'rust cli todo app yap' to build", () => {
    const r = classifyByHeuristic("rust cli todo app yap");
    expect(r?.mode).toBe("build");
  });

  it("routes CS-domain keyword (snake game) to build without verb", () => {
    const r = classifyByHeuristic("snake game in html");
    expect(r?.mode).toBe("build");
  });

  it("routes code fence to build", () => {
    const r = classifyByHeuristic(
      "Bu kodda hata var:\n```python\ndef foo(): return 1\n```",
    );
    expect(r?.mode).toBe("build");
  });

  it("routes 'typo' / 'rename' to quickcode", () => {
    expect(classifyByHeuristic("fix typo in user.py")?.mode).toBe("quickcode");
    expect(classifyByHeuristic("rename foo to bar")?.mode).toBe("quickcode");
  });
});


describe("classifyByHeuristic — question stems", () => {
  it("routes 'ne / nedir / nasıl' to research", () => {
    // NB: questions ABOUT Amor ("amor ne yapar") route to chat (identity),
    // not research — so use generic factual questions here.
    expect(classifyByHeuristic("fotosentez ne işe yarar")?.mode).toBe("research");
    expect(classifyByHeuristic("blockchain nedir")?.mode).toBe("research");
    expect(classifyByHeuristic("nasıl çalışır")?.mode).toBe("research");
  });

  it("routes 'how / what' to research", () => {
    expect(classifyByHeuristic("how to deploy")?.mode).toBe("research");
    expect(classifyByHeuristic("what is a CRDT")?.mode).toBe("research");
  });

  it("routes 'why does X break' to research (factual question)", () => {
    expect(classifyByHeuristic("why does this break")?.mode).toBe("research");
  });

  it("routes plain question (ends with ?) to research", () => {
    expect(classifyByHeuristic("docker container niye düşüyor?")?.mode).toBe(
      "research",
    );
  });
});


describe("classifyByHeuristic — chitchat / greetings", () => {
  // Cycle UI v2.9 — greetings/chitchat now route to the fast "chat"
  // lane (was "thinking", the slow 6-phase pipeline).
  it("routes 'merhaba' / 'selam' / 'nasılsın' to chat", () => {
    expect(classifyByHeuristic("merhaba")?.mode).toBe("chat");
    expect(classifyByHeuristic("selam")?.mode).toBe("chat");
    expect(classifyByHeuristic("nasılsın")?.mode).toBe("chat");
    expect(classifyByHeuristic("naber")?.mode).toBe("chat");
  });

  it("routes 'hi / hello / how are you' to chat", () => {
    expect(classifyByHeuristic("hi there")?.mode).toBe("chat");
    expect(classifyByHeuristic("how are you")?.mode).toBe("chat");
  });

  it("routes 'teşekkür / thanks' to chat", () => {
    expect(classifyByHeuristic("teşekkür ederim")?.mode).toBe("chat");
    expect(classifyByHeuristic("thanks")?.mode).toBe("chat");
  });

  it("routes identity questions ('sen kimsin' / 'what can you do') to chat", () => {
    expect(classifyByHeuristic("sen kimsin")?.mode).toBe("chat");
    expect(classifyByHeuristic("what can you do")?.mode).toBe("chat");
    expect(classifyByHeuristic("amor ne yapar")?.mode).toBe("chat");
  });
});


describe("classifyByHeuristic — deep think", () => {
  it("routes 'compare / tradeoff' to thinking", () => {
    expect(
      classifyByHeuristic("compare CRDT vs OT")?.mode,
    ).toBe("thinking");
    expect(
      classifyByHeuristic("rust vs go tradeoff")?.mode,
    ).toBe("thinking");
  });

  it("routes bare 'X vs Y' compare prompts to thinking", () => {
    expect(classifyByHeuristic("react vs vue 2026")?.mode).toBe("thinking");
    expect(classifyByHeuristic("postgres ile mongo karşılaştır")?.mode).toBe(
      "thinking",
    );
  });
});


describe("classifyByHeuristic — multi-step / consortium", () => {
  it("routes 'plan proje' / 'full system' to consortium", () => {
    expect(
      classifyByHeuristic("plan proje son ürünü çıkar")?.mode,
    ).toBe("consortium");
    expect(
      classifyByHeuristic("build a full system")?.mode,
    ).toBe("consortium");
  });
});


describe("classifyByHeuristic — fallback (return null)", () => {
  it("returns null for empty input", () => {
    expect(classifyByHeuristic("")).toBeNull();
    expect(classifyByHeuristic("   ")).toBeNull();
  });

  it("returns null for genuinely ambiguous prompts", () => {
    // No URL, no code keyword, no question stem, no chitchat marker
    // → classifier should decide.
    expect(classifyByHeuristic("blueberry pancake recipe steel cut oats")).toBeNull();
  });
});


describe("classifyByHeuristic — confidence", () => {
  it("emits confidence ≥0.85 on every hit", () => {
    const prompts = [
      "https://example.com",
      "python ile fibonacci yaz",
      "snake game in html",
      "amor ne yapar",
      "merhaba",
      "how to deploy a docker container",
    ];
    for (const p of prompts) {
      const r = classifyByHeuristic(p);
      expect(r).not.toBeNull();
      expect(r!.confidence).toBeGreaterThanOrEqual(0.85);
    }
  });
});
