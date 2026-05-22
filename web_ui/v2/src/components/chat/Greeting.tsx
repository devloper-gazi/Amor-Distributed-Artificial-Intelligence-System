/**
 * Cycle UI v2.6 (Karar B) — time-of-day-conditional, name-personalised
 * greeting shown on the empty chat state.
 *
 * Pattern mirrors Gemini 2026 redesign ("Hi {name}, what's on your
 * mind?" centered above composer) and Claude.ai's rotating greeting
 * pool ("Good evening, {name}").  We do mode-agnostic only — AMOR's
 * 6 modes mean a mode-aware greeting would force the user into a
 * choice they haven't made yet (the classifier hasn't seen prompt
 * yet).
 *
 * Tipografi: `clamp(28px, 4.4vw, 44px)`, weight 500, tight tracking,
 * max-width 38ch — fits two lines on mobile, single line on desktop.
 * System font stack (no CDN font fetch).
 *
 * i18n keys (`auth.tag.*` namespace already exists from v2.5 Login;
 * we add a parallel `greet.*` namespace in D6):
 *   * greet.morning   (05:00-11:59)  — "Günaydın, {name}"
 *   * greet.afternoon (12:00-17:59)  — "İyi öğleden sonra, {name}"
 *   * greet.evening   (18:00-22:59)  — "İyi akşamlar, {name}"
 *   * greet.night     (23:00-04:59)  — "Hadi başlayalım, {name}"
 *   * greet.fallback_name             — "orada" (when no user name)
 *
 * Turkish casing: `localeUpper("İyi akşamlar")` returns "İYİ AKŞAMLAR"
 * (the heading itself is NOT uppercased — only the optional eyebrow
 * label if any).  Names rendered as-is.
 *
 * Motion: `.amor-enter` keyframe (200ms ease-out, opacity + translateY)
 * runs once on mount — feels like the page just gave way to the
 * user.
 */
import { type Component, createMemo } from "solid-js";
import { t } from "../../i18n";
import { auth } from "../../lib/auth";

/** Pick the greeting key based on local clock.  Pure function so the
 *  test surface is trivial. */
function greetingKey(hour: number): string {
  if (hour >= 5 && hour < 12)  return "greet.morning";
  if (hour >= 12 && hour < 18) return "greet.afternoon";
  if (hour >= 18 && hour < 23) return "greet.evening";
  return "greet.night";
}

/** Prefer the user's display name; fall back to the localised
 *  generic ("orada" / "there") when unauthenticated or anonymous. */
function pickName(): string {
  const u = auth.user();
  if (u) {
    if (u.display_name && u.display_name.trim()) return u.display_name.trim();
    if (u.username && u.username.trim()) return u.username.trim();
  }
  return t("greet.fallback_name");
}

export const Greeting: Component = () => {
  // Re-evaluate on every render so a long-running tab that crosses
  // midnight still gets the right greeting.  Cheap — single Date()
  // per mount + per re-render trigger.
  const key  = createMemo(() => greetingKey(new Date().getHours()));
  const name = createMemo(() => pickName());

  return (
    <h1
      // Cycle UI v2.6.3 — Greeting daha hafif Gemini "What should we
      // focus on?" tarzı: clamp(22-32px), weight 400 (regular),
      // text-subtle (display'den daha yumuşak), tight tracking.
      // Card composer'ın altındaki rolü hero'dan invite-line'a düştü.
      class="amor-enter text-center font-normal tracking-tight text-text-display"
      style={{
        "font-size": "clamp(1.5rem, 3.2vw, 2rem)",
        "line-height": "1.2",
        "letter-spacing": "-0.015em",
        "max-width": "38ch",
        "margin-left": "auto",
        "margin-right": "auto",
      }}
      data-amor-greeting=""
    >
      {t(key(), { name: name() })}
    </h1>
  );
};

export default Greeting;
