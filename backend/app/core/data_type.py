"""
Column type classification: assigns each column a coarse "main type"
(numeric or string - the same split column_profiler uses to decide which
metric groups to run) and a finer "secondary type" describing what kind of
data it actually holds (integer, binary, date, categorical, textual,
array, ...).

Ported from an earlier project's classifier (same heuristics/thresholds),
with three adaptations:
  - No scikit-learn dependency: the English stopword set used for text
    detection is inlined below (a static, public list) rather than pulling
    in scikit-learn just for one constant.
  - Added a string-boolean heuristic (values are all "true"/"false" or
    "yes"/"no", case-insensitive). This app's CSV preprocessing never
    produces a real pandas bool dtype - every column starts as a string and only
    gets coerced to numeric or left as object - so the original dtype-only
    boolean check (pd.api.types.is_bool_dtype) never fires here. Without
    this, a column like the sample data's products.active ("true"/"FALSE"/
    "True", mixed case) would fall through to a generic "categorical".
  - Added a minimum word count (MIN_WORDS_FOR_TEXT_LINE) before a sampled
    line can count as "text-like" at all. Without it, short identifying
    phrases - book/movie/song titles, names - get misclassified as
    "textual" (and then excluded from discovery entirely, see quality.py's
    UNUSABLE_SECONDARY_TYPES) almost every time: a title like "The Lord of
    the Rings" tokenizes to [the, lord, of, the, rings], and "the"/"the"/
    "of" being stopwords makes that 3/5 = 60% - far past
    STOPWORD_THRESHOLD - even though the string itself is short and
    plainly not prose. The stopword-ratio check is inherently biased
    against short phrases (a couple of filler words dominates a 4-5 token
    ratio in a way they never would in an actual paragraph), so titles
    need to be excluded from that check on length grounds before it ever
    runs, not tuned around after the fact.
"""
from __future__ import annotations

import ast
import re
import warnings
from typing import Any

import pandas as pd

STOPWORD_THRESHOLD = 0.2  # Fraction of stopwords in a sample line to count it as "text-like".
LEN_THRESHOLD = 40  # Line length (chars) above which a line counts as "text-like" regardless of stopwords.
MIN_WORDS_FOR_TEXT_LINE = 12  # Below this word count, a line is a short phrase (title/name), never "text-like".
TEXT_MATCH_THRESHOLD = 0.5  # Fraction of sampled lines that must be "text-like" for the column to be textual.
MAX_UNIQUE_FOR_CATEGORICAL = 20  # Max distinct values for a column to be considered categorical.
SAMPLE_SIZE = 1000

BOOLEAN_VOCABULARIES = [{"true", "false"}, {"yes", "no"}]

# Which main type (numeric vs string) each secondary type rolls up into -
# this is what column_profiler uses to pick which metric groups to run.
MAIN_TYPES = {
    "integer": "numeric",
    "float": "numeric",
    "binary": "numeric",
    "categorical_with_numbers": "numeric",
    "boolean": "string",
    "categorical": "string",
    "textual": "string",
    "date": "string",
    "array": "string",
    "other": "string",
    "string": "string",
}

ENGLISH_STOP_WORDS = frozenset({
    "a", "about", "above", "across", "after", "afterwards", "again", "against", "all", "almost", "alone", "along",
    "already", "also", "although", "always", "am", "among", "amongst", "amoungst", "amount", "an", "and", "another",
    "any", "anyhow", "anyone", "anything", "anyway", "anywhere", "are", "around", "as", "at", "back", "be", "became",
    "because", "become", "becomes", "becoming", "been", "before", "beforehand", "behind", "being", "below",
    "beside", "besides", "between", "beyond", "bill", "both", "bottom", "but", "by", "call", "can", "cannot",
    "cant", "co", "con", "could", "couldnt", "cry", "de", "describe", "detail", "do", "done", "down", "due",
    "during", "each", "eg", "eight", "either", "eleven", "else", "elsewhere", "empty", "enough", "etc", "even",
    "ever", "every", "everyone", "everything", "everywhere", "except", "few", "fifteen", "fifty", "fill", "find",
    "fire", "first", "five", "for", "former", "formerly", "forty", "found", "four", "from", "front", "full",
    "further", "get", "give", "go", "had", "has", "hasnt", "have", "he", "hence", "her", "here", "hereafter",
    "hereby", "herein", "hereupon", "hers", "herself", "him", "himself", "his", "how", "however", "hundred", "i",
    "ie", "if", "in", "inc", "indeed", "interest", "into", "is", "it", "its", "itself", "keep", "last", "latter",
    "latterly", "least", "less", "ltd", "made", "many", "may", "me", "meanwhile", "might", "mill", "mine", "more",
    "moreover", "most", "mostly", "move", "much", "must", "my", "myself", "name", "namely", "neither", "never",
    "nevertheless", "next", "nine", "no", "nobody", "none", "noone", "nor", "not", "nothing", "now", "nowhere",
    "of", "off", "often", "on", "once", "one", "only", "onto", "or", "other", "others", "otherwise", "our", "ours",
    "ourselves", "out", "over", "own", "part", "per", "perhaps", "please", "put", "rather", "re", "same", "see",
    "seem", "seemed", "seeming", "seems", "serious", "several", "she", "should", "show", "side", "since",
    "sincere", "six", "sixty", "so", "some", "somehow", "someone", "something", "sometime", "sometimes",
    "somewhere", "still", "such", "system", "take", "ten", "than", "that", "the", "their", "them", "themselves",
    "then", "thence", "there", "thereafter", "thereby", "therefore", "therein", "thereupon", "these", "they",
    "thick", "thin", "third", "this", "those", "though", "three", "through", "throughout", "thru", "thus", "to",
    "together", "too", "top", "toward", "towards", "twelve", "twenty", "two", "un", "under", "until", "up", "upon",
    "us", "very", "via", "was", "we", "well", "were", "what", "whatever", "when", "whence", "whenever", "where",
    "whereafter", "whereas", "whereby", "wherein", "whereupon", "wherever", "whether", "which", "while", "whither",
    "who", "whoever", "whole", "whom", "whose", "why", "will", "with", "within", "without", "would", "yet", "you",
    "your", "yours", "yourself", "yourselves",
})


def is_text_column_heuristic(
    series: pd.Series, stopword_threshold: float = STOPWORD_THRESHOLD, avg_len_threshold: int = LEN_THRESHOLD
) -> bool:
    series = series.dropna().astype(str)
    if series.empty:
        return False

    sample = series.sample(min(50, len(series)), random_state=42)
    match_count = 0
    for line in sample:
        tokens = re.findall(r'\b\w+\b', line.lower())
        if len(tokens) < MIN_WORDS_FOR_TEXT_LINE:
            # Too short a phrase to be prose regardless of content - a
            # title or name, not "text" (see module docstring: a few
            # stopwords in a short title dominates the ratio below in a
            # way they never would in an actual paragraph).
            continue
        stopword_ratio = sum(t in ENGLISH_STOP_WORDS for t in tokens) / len(tokens)
        if stopword_ratio > stopword_threshold or len(line) > avg_len_threshold:
            match_count += 1

    return match_count / len(sample) > TEXT_MATCH_THRESHOLD


def is_probable_array(val: Any) -> bool:
    if isinstance(val, list):
        return True
    if isinstance(val, str) and val.strip().startswith("["):
        try:
            parsed = ast.literal_eval(val)
            return isinstance(parsed, (list, tuple))
        except (ValueError, SyntaxError):
            return False
    return False


def _is_string_boolean(series: pd.Series) -> bool:
    values = set(series.astype(str).str.strip().str.lower().unique())
    values.discard("")
    if not values:
        return False
    return any(values.issubset(vocab) for vocab in BOOLEAN_VOCABULARIES)


def classify_secondary_type(column: pd.Series, sample_size: int = SAMPLE_SIZE) -> str:
    column_sample = column.sample(n=sample_size, random_state=0) if len(column) > sample_size else column
    series = column_sample.dropna()
    if series.empty:
        return "other"

    if pd.api.types.is_bool_dtype(series):
        return "boolean"

    if pd.api.types.is_integer_dtype(series):
        unique_vals = pd.unique(series)
        if len(unique_vals) == 2 and set(unique_vals).issubset({0, 1}):
            return "binary"
        if len(unique_vals) <= MAX_UNIQUE_FOR_CATEGORICAL:
            return "categorical_with_numbers"
        return "integer"

    if pd.api.types.is_float_dtype(series):
        return "float"

    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"

    # Everything past this point arrived as strings: this app's preprocessing
    # normalizes dates to ISO strings rather than a real datetime64 dtype,
    # and there's no bool coercion, so both need heuristic detection here.
    if _is_string_boolean(series):
        return "boolean"

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            pd.to_datetime(series, errors="raise", format="mixed")
        return "date"
    except Exception:
        pass

    unique_vals = pd.unique(series)
    if all(is_probable_array(x) for x in unique_vals):
        return "array"
    if pd.Series(unique_vals).nunique() <= MAX_UNIQUE_FOR_CATEGORICAL:
        return "categorical"
    if series.astype(str).str.contains("base64").any():
        return "other"  # Possibly a blob (image/audio/etc.) encoded as text.
    if is_text_column_heuristic(series):
        return "textual"
    return "string"


def classify_column(column: pd.Series, sample_size: int = SAMPLE_SIZE) -> dict[str, Any]:
    secondary = classify_secondary_type(column, sample_size)
    return {"main_type": MAIN_TYPES[secondary], "secondary_type": secondary}
