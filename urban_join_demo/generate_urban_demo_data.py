"""
Generates a small urban-development data lake purpose-built to demonstrate
this app's full pipeline end to end (see README.md "Autonomous gold-layer
construction", "Detecting redundant column groups", "Detecting mostly-null
columns") - five CSVs, with the *values* deliberately correlated (or not)
so every declutter signal, both DIADA curation modes, join_builder's
aggregation path, and the autonomous/manual candidate-selection divergence
all have something real to find, not just plausible-looking schemas.
Replaces retail_join_demo as the paper's running example; that generator
and its data (scripts/generate_join_demo_data.py) have since been removed
from the repo.

Demo entry point: in the UI, pick property_listings.property_id as the
base column for join discovery, and include all four candidates
(district_properties, weather_stations, permit_applications,
resident_reviews).

  - property_listings.csv (BASE) - one row per property. property_id is
    the real key everything else joins through.
  - district_properties.csv (TRUE candidate) - one row per postal_code;
    tax_rate/school_quality/crime_rate *actually drive* market_value (see
    value_score below), so after joining, DIADA finds a real relationship
    and "Structure dataset" clusters them with the base's own
    squared_m/num_rooms.
  - weather_stations.csv (SPURIOUS candidate, but well-covered) - one row
    per property, with station_id reusing property_id's own value domain
    verbatim (a realistic data-lake artifact: some source system relabeled
    its own row identifier as "station_id"). This is what makes it
    profile-identical to property_id - join discovery cannot tell it apart
    from a real key - while still being a semantically nonsensical join (a
    property and a weather reading are unrelated entities). Its four
    monthly_* columns are mutually self-consistent (driven by a shared
    climate_score) but explicitly orthogonalized against value_score, so
    "Structure dataset" isolates them into their own cluster, disconnected
    from the price cluster - full row coverage (unlike permit_applications
    below) means DIADA sees plenty of data to confidently rule this out,
    rather than just running out of values to compare.
  - permit_applications.csv (TRUE candidate, sparse) - only a handful of
    properties (<1%) have ever filed a permit, so after a left join
    nearly every row is null for its columns: this is what
    join_builder.mostly_null_columns() is for - a *genuinely correct* join
    (property_id really does match) that's still useless because so few
    rows have it. permit_id is permit_applications' own primary key but
    isn't the column anything was joined on, so it separately demonstrates
    join_builder.unused_primary_key_columns().
  - resident_reviews.csv (TRUE candidate, needs aggregation) - three
    reviews per property (every property has some, unlike the sparse
    permits above), so property_id is only ~33% unique within its own
    file - well below catalog.PRIMARY_KEY_UNIQUENESS_THRESHOLD (0.90).
    review_rating/sentiment_score are driven by the *same* value_score as
    market_value, so a human manually including it (join_builder averages
    the three reviews per property down to one row first - the
    aggregation path) gets a real, join-worthy signal that lands in the
    price cluster alongside district_properties. But because its own
    property_id isn't unique enough, autonomous mode's stricter candidate
    filter (gold_layer._candidates_for_base) excludes it automatically -
    the same "well-covered but not unique enough to autonomously trust"
    fate as retail_join_demo's customer_reviews, deliberately reproduced
    here so the autonomous/manual divergence is visible in this lake too,
    not just the retail one.
  - Every file also carries at least one deliberately unrelated "noise"
    column (locker_number, inspector_name, flagged_for_moderation, ...)
    that "Remove univariate noise" should drop, and wall_color/wall_color_hex
    in property_listings are a deliberately near-bijective pair (a color
    name and its hex code) that join_builder.redundant_column_groups()
    should catch - and, because it's caught by declutter, DIADA's CF2
    never even gets to cluster them together.

All relationship strengths below were tuned empirically against the actual
DIADA jar (not assumed from Pearson correlation alone) until the
connected-components clustering came out clean and stable - see this
script's development notes for the exact verification run.

Usage: python generate_urban_demo_data.py [output_dir]
(default output_dir: ../urban_join_demo, relative to this script)
"""
import os
import random
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
from faker import Faker

fake = Faker()
Faker.seed(11)
random.seed(11)
np.random.seed(11)

OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "urban_join_demo")
os.makedirs(OUT_DIR, exist_ok=True)

N_PROPERTIES = 3000
N_DISTRICTS = 50
N_PERMITS = 20  # << 1% of N_PROPERTIES, well past MOSTLY_NULL_THRESHOLD (0.99) once left-joined
N_REVIEWS_PER_PROPERTY = 3  # every property has some - >> PRIMARY_KEY_UNIQUENESS_THRESHOLD's complement, so still needs aggregating

property_ids = [f"PR-{i:05d}" for i in range(1, N_PROPERTIES + 1)]
# Prefixed rather than a bare zero-padded number (e.g. "08025") - a
# pure-digit code gets silently numeric-coerced by data_loader's
# numeric-look-alike heuristic (leading zeros and all), which would make
# it ineligible as a string-typed join key for the autonomous pipeline's
# per-base-column discovery loop (see module docstring / catalog.py).
postal_codes = [f"PC-{i:03d}" for i in range(1, N_DISTRICTS + 1)]


def normalize(arr: np.ndarray) -> np.ndarray:
    lo, hi = arr.min(), arr.max()
    return (arr - lo) / (hi - lo) if hi > lo else np.zeros_like(arr)


def orthogonalize(x: np.ndarray, score: np.ndarray) -> np.ndarray:
    """Removes any linear relationship between x and score. OLS residuals
    are orthogonal to the regressor by construction, so this guarantees
    x ends up uncorrelated with score in *this* sample - unlike drawing x
    independently and hoping, which at a few thousand points can easily
    land on a misleadingly non-trivial correlation by chance."""
    slope, intercept = np.polyfit(score, x, 1)
    return x - (slope * score + intercept) + x.mean()


def write_csv(filename: str, df: pd.DataFrame) -> None:
    df.to_csv(os.path.join(OUT_DIR, filename), index=False)


def random_date(start_days_ago: int = 730, end_days_ago: int = 0) -> str:
    d = date.today() - timedelta(days=random.randint(end_days_ago, start_days_ago))
    return d.isoformat()


# ---- district_properties.csv (TRUE candidate) ------------------------------
tax_rate = np.round(np.random.uniform(0.5, 3.5, N_DISTRICTS), 2)
school_quality = np.round(np.random.uniform(2, 10, N_DISTRICTS), 1)
crime_rate = np.round(np.random.uniform(5, 80, N_DISTRICTS), 1)

# The three district metrics are drawn independently of each other (no
# shared random component) - deliberately, so district_desirability's
# predictive power over market_value comes from genuinely combining three
# separate signals, not from three redundant measurements of the same
# underlying thing. Each metric individually still correlates with
# market_value (see property_desirability's weight in value_score below) -
# confirmed empirically: individual |corr| ~0.3-0.45, weaker on their own
# than the base's own squared_m/num_rooms (~0.70-0.73) now that both size
# and district genuinely drive value_score at strong weights, but each
# still real and unambiguous enough on its own for DIADA to pick up -
# which is the point: "Structure dataset" clusters market_value with all
# five of squared_m/num_rooms/tax_rate/school_quality/crime_rate together,
# not just whichever one happens to correlate hardest.
district_desirability = normalize(
    0.34 * normalize(tax_rate) + 0.33 * normalize(school_quality) + 0.33 * normalize(-crime_rate)
)

district_properties_df = pd.DataFrame({
    "postal_code": postal_codes,
    "tax_rate": tax_rate,
    "school_quality": school_quality,
    "crime_rate": crime_rate,
})
write_csv("district_properties.csv", district_properties_df)

# ---- property_listings.csv (BASE) ------------------------------------------
property_postal_code = np.random.choice(postal_codes, N_PROPERTIES)
district_idx = {pc: i for i, pc in enumerate(postal_codes)}
property_desirability = np.array([district_desirability[district_idx[pc]] for pc in property_postal_code])

squared_m = np.round(np.random.uniform(35, 220, N_PROPERTIES), 1)
num_rooms = np.round(np.clip(squared_m / 30 + np.random.normal(0, 0.6, N_PROPERTIES), 1, 8)).astype(int)

# Both size (squared_m/num_rooms) and district_desirability drive
# value_score at genuinely strong individual weights (not the earlier
# 0.15/0.10/0.75 split, which correlated market_value with squared_m/
# num_rooms individually at only ~0.4 each - real, but too weak for DIADA's
# soundness score to clear SOUNDNESS_THRESHOLD against market_value, so
# "Structure dataset" isolated size into its own cluster even though it
# genuinely belongs with the price cluster; confirmed directly by
# rerunning the same join+curate with this weighting bumped up).
# squared_m's own weight is 0.50 specifically (not just raised to match
# district's 0.75) so squared_m/num_rooms's ~0.94 mutual correlation with
# each other doesn't make them dominate value_score outright now that
# they're strong too - district's three individual metrics still land
# ~0.3-0.45 each with market_value this way (down from ~0.4-0.6, but each
# still real and unambiguous on its own), squared_m/num_rooms land
# ~0.70-0.73, and "Structure dataset" clusters market_value with all five
# of squared_m/num_rooms/tax_rate/school_quality/crime_rate together - the
# single price cluster this demo was always meant to produce (see module
# docstring).
value_score = normalize(
    0.50 * normalize(squared_m) + 0.10 * normalize(num_rooms) + 0.75 * property_desirability
)
market_value = np.round(60000 + value_score * 550000 + np.random.normal(0, 4000, N_PROPERTIES), 2).clip(min=30000)

wall_colors = ["Ivory White", "Sage Green", "Slate Gray", "Terracotta", "Sky Blue",
               "Charcoal Black", "Sand Beige", "Blush Pink", "Forest Green", "Mustard Yellow"]
wall_color_hex_map = {
    "Ivory White": "#FFFFF0", "Sage Green": "#9CAF88", "Slate Gray": "#708090",
    "Terracotta": "#E2725B", "Sky Blue": "#87CEEB", "Charcoal Black": "#36454F",
    "Sand Beige": "#F4E1C1", "Blush Pink": "#F9C6C9", "Forest Green": "#228B22",
    "Mustard Yellow": "#E1AD01",
}
wall_color = [random.choice(wall_colors) for _ in range(N_PROPERTIES)]

property_listings_df = pd.DataFrame({
    "property_id": property_ids,
    "postal_code": property_postal_code,
    "market_value": market_value,
    "squared_m": squared_m,
    "num_rooms": num_rooms,
    "wall_color": wall_color,
    # Redundant with wall_color (near-bijective: each name maps to exactly
    # one hex code) - join_builder.redundant_column_groups() should catch
    # this pair before DIADA ever sees both.
    "wall_color_hex": [wall_color_hex_map[c] for c in wall_color],
    # Noise: an arbitrary basement-locker assignment, unrelated to
    # anything (and numeric, so it's plain CF1 noise, not a declutter
    # signal - see catalog.py's numeric-coincidence caveat).
    "locker_number": np.random.randint(1, 300, N_PROPERTIES),
})
write_csv("property_listings.csv", property_listings_df)

# ---- weather_stations.csv (SPURIOUS candidate, but well-covered) -----------
# station_id deliberately reuses property_id's own value domain verbatim -
# see module docstring for why. Every property gets exactly one row, so
# after the join every monthly_* column is fully populated (unlike
# permit_applications below), giving DIADA enough data to confidently
# determine "self-consistent, but unrelated to price" rather than just
# running out of values to compare.
climate_score = normalize(np.random.normal(0, 1, N_PROPERTIES))

monthly_temp = np.round(orthogonalize(10 + climate_score * 15 + np.random.normal(0, 0.6, N_PROPERTIES), value_score), 1)
monthly_precipitations = np.round(
    orthogonalize(np.clip(80 - climate_score * 40 + np.random.normal(0, 3, N_PROPERTIES), 0, None), value_score), 1
)
monthly_wind_speed = np.round(orthogonalize(10 + climate_score * 4 + np.random.normal(0, 0.4, N_PROPERTIES), value_score), 1)
monthly_humidity = np.round(
    np.clip(orthogonalize(50 + climate_score * 20 + np.random.normal(0, 1.2, N_PROPERTIES), value_score), 5, 100), 1
)

weather_stations_df = pd.DataFrame({
    "station_id": property_ids,
    "monthly_temp": monthly_temp,
    "monthly_precipitations": monthly_precipitations,
    "monthly_wind_speed": monthly_wind_speed,
    "monthly_humidity": monthly_humidity,
})
write_csv("weather_stations.csv", weather_stations_df)

# ---- permit_applications.csv (TRUE candidate, sparse) -----------------------
permit_property_ids = random.sample(property_ids, N_PERMITS)
permit_types = ["Renovation", "Extension", "Demolition", "New Construction"]
permit_statuses = ["Approved", "Pending", "Rejected"]


# A handful of named inspectors, reused across permits - not one distinct
# name per row. Drawing a fresh fake.name() per row would, at only
# N_PERMITS rows, come out unique by pure chance almost every time,
# coincidentally tripping both the unused-primary-key and redundant-column
# declutter signals against permit_id (a spurious small-sample artifact,
# not the thing either signal is meant to catch - see this script's
# verification notes). A small fixed pool is also just more realistic: a
# city department has a handful of building inspectors, not one per permit.
inspector_pool = [fake.name() for _ in range(5)]

permit_applications_df = pd.DataFrame({
    # permit_applications' own primary key - never the join key (property_id
    # is), so join_builder.unused_primary_key_columns() should flag it.
    "permit_id": [f"PMT-{i:05d}" for i in range(1, N_PERMITS + 1)],
    "property_id": permit_property_ids,
    "permit_type": [random.choice(permit_types) for _ in range(N_PERMITS)],
    "permit_status": [random.choice(permit_statuses) for _ in range(N_PERMITS)],
    # Noise: unrelated to the permit's own type/status or the property.
    "inspector_name": [random.choice(inspector_pool) for _ in range(N_PERMITS)],
})
write_csv("permit_applications.csv", permit_applications_df)

# ---- resident_reviews.csv (TRUE candidate, needs aggregation) --------------
# Three reviews per property (every property has some), so property_id is
# only ~33% unique in this file - well under the 90% bar
# gold_layer._candidates_for_base requires of an autonomous candidate (see
# module docstring). review_rating/sentiment_score are driven by the same
# value_score as market_value, so a human manually joining this in gets a
# real signal - join_builder averages the three reviews per property down
# to one row first (the aggregation path) - while autonomous mode skips it
# entirely on its own, exactly like retail_join_demo's customer_reviews.
# Generated last (not interleaved with the datasets above) so it doesn't
# perturb the shared random-number stream those already-tuned datasets
# depend on - see this script's verification notes.
review_rows = []
rv_counter = 1
for p_idx, pid in enumerate(property_ids):
    v = value_score[p_idx]
    for _ in range(N_REVIEWS_PER_PROPERTY):
        rating = int(np.clip(round(1 + 4 * v + np.random.normal(0, 0.4)), 1, 5))
        sentiment_score = round(float(np.clip(v * 100 + np.random.normal(0, 6), 0, 100)), 1)
        review_rows.append({
            "review_id": f"RV-{rv_counter:06d}",
            "property_id": pid,
            "review_rating": rating,
            "sentiment_score": sentiment_score,
            "review_date": random_date(),
            # Noise: whether the review happened to be flagged for manual
            # moderation, unrelated to its content or the property's value.
            "flagged_for_moderation": random.choice([True, False]),
        })
        rv_counter += 1

write_csv("resident_reviews.csv", pd.DataFrame(review_rows))

ALL_FILES = [
    "property_listings.csv", "district_properties.csv", "weather_stations.csv",
    "permit_applications.csv", "resident_reviews.csv",
]

print(f"Wrote {len(ALL_FILES)} CSV files to {os.path.abspath(OUT_DIR)}")
for fn in ALL_FILES:
    path = os.path.join(OUT_DIR, fn)
    with open(path) as f:
        n = sum(1 for _ in f) - 1
    print(f"  {fn}: {n} rows")
