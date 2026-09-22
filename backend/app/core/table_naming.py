"""
Cheap, local, deterministic naming for the gold layer's grouped mode:
group_display_name names a joined table after its member datasets.

This module used to also generate a display name for each DIADA CF2
cluster from its own column names (via a local TF-IDF over column-name
tokens, or later a whole-column-scoring variant of the same idea) - see
git history / docs/ad_hoc_gold_layer.md for both attempts and why they
were dropped. Every local, deterministic approach tried ended up either
inventing unreadable word-mashups (e.g. "rate_crime" from tax_rate +
crime_rate, sharing "rate") or, once fixed to only ever emit real column
names, still just concatenating them ("crime_rate+market_value") rather
than producing an actually coherent name - which needs real language
understanding, not statistics over column-name substrings. Generating one
via an LLM per cluster was considered and rejected for the same reason it
was rejected originally: real per-table cost and latency on every
autonomous run, for something that happens dozens of times per build.
Rather than keep shipping a "smart-looking" name that isn't reliably
coherent, CF2 clusters are now labeled plainly ("Cluster 1", "Cluster 2",
...) - see gold_layer.py's _cf2_tables - and the columns themselves (shown
alongside every table) are what actually convey what's in it.
"""
from __future__ import annotations


def group_display_name(members: list[str], max_shown: int = 3) -> str:
    """Sorted, "+"-joined dataset names, truncated for readability once a
    group gets large - "a+b+c" for a small group, "a+b+c and 2 more" beyond
    max_shown. Display-only: a caller needing a stable, always-unique key
    (e.g. a GoldTable id) should use the full sorted "+".join(members)
    instead, never this potentially-truncated name."""
    ordered = sorted(members)
    if len(ordered) <= max_shown:
        return "+".join(ordered)
    shown = "+".join(ordered[:max_shown])
    remaining = len(ordered) - max_shown
    return f"{shown} and {remaining} more"
