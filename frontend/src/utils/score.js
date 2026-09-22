// Shared joinability-score helpers - the raw score joinability_model.py
// produces is a GradientBoostingRegressor prediction with no natural
// range of its own (see app/joinability_model.py:_score_bounds), so on its
// own it reads as an arbitrary, hard-to-compare small fraction. Every UI
// that shows one normalizes it into a 0-1 range instead, against [0,
// bounds.max] - bounds ({min, max}, fetched from the backend via
// /api/join-discovery and /api/catalog) is the model's true global
// min/max, but bounds.min is deliberately NOT used as the normalization
// floor: it's a real bound (every tree hitting its own single most
// extreme leaf at once), but not a reachable one - no real feature
// combination gets anywhere near it. Anchoring normalization there was
// confirmed (against a real project's actual scores) to collapse the
// entire practical range into a narrow band: ~75% of real candidate pairs
// are noise scored within a hair of 0, and under a [bounds.min, bounds.
// max] normalization every one of them landed at the same 0.301 - indis-
// tinguishable from each other, and barely below a *genuine* match's
// 0.361. 0 is both simpler and a much better floor: it's where a raw
// score of exactly 0 (and everything at or below it, i.e. "no better
// than baseline") already sits, so noise reads as ~0% and real signal
// actually reads as visibly higher, while bounds.max stays fixed so
// scores remain comparable across different projects.

export function formatRawScore(score) {
  return score.toFixed(6)
}

export function normalizeScore(score, bounds) {
  if (!bounds || bounds.max <= 0) return 0
  return Math.min(1, Math.max(0, score / bounds.max))
}

export function formatNormalizedScore(score, bounds) {
  return normalizeScore(score, bounds).toFixed(3)
}
