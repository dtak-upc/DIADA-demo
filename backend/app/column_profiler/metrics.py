"""
Metric group definitions: each class builds one DuckDB SQL query and knows
how to turn its raw result into final values.

Ported from an earlier project's profiling engine (same metric names and
formulas) so results stay comparable with work already done there - in
particular, a joinability model trained elsewhere consumes these exact
feature names. `Metric.normalize`/`distance_pattern` aren't used yet, but
are kept for that reason: a future join-candidate scoring stage is the
likely next consumer, and it needs this metadata.
"""
import math

import jellyfish


def _nan_to_zero(value):
    """DuckDB's SKEWNESS/KURTOSIS return NaN (not NULL) when variance is
    zero - e.g. a column where every value is unique, so all frequency
    counts are identical. NaN survives everywhere in Python but isn't valid
    JSON, so it has to be swapped out before this reaches the API response."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0
    return value


class Metric:
    def __init__(self, name, normalize, distance_pattern):
        self.name = name
        self.normalize = normalize
        self.distance_pattern = distance_pattern


class CardinalityMetrics:
    def __init__(self):
        self.cardinality = Metric('cardinality', True, "substraction")
        self.incompleteness = Metric('incompleteness', False, "substraction")
        self.uniqueness = Metric('uniqueness', False, "substraction")
        self.entropy = Metric('entropy', True, "substraction")
        self.num_rows = Metric('num_rows', True, "substraction")

        self.metrics_list = [self.cardinality, self.incompleteness, self.uniqueness, self.entropy, self.num_rows]

    def build_query(self, column, table):
        return f"""
            SELECT
                COUNT(*) AS {self.num_rows.name},
                COUNT(DISTINCT "{column}") AS {self.cardinality.name},
                ENTROPY("{column}") AS {self.entropy.name},
                COUNT(DISTINCT "{column}") AS {self.uniqueness.name},
                COUNT(*) - COUNT("{column}") AS {self.incompleteness.name}
            FROM "{table}"
        """

    def process_result(self, result, count_rows):
        for key in (self.uniqueness.name, self.incompleteness.name):
            result[key] = result[key] / count_rows if count_rows > 0 else 0
        return result


class DistributionMetrics:
    def __init__(self):
        self.frequency_avg = Metric('frequency_avg', True, "substraction")
        self.frequency_min = Metric('frequency_min', True, "substraction")
        self.frequency_max = Metric('frequency_max', True, "substraction")
        self.frequency_sd = Metric('frequency_sd', True, "substraction")
        self.frequency_iqr = Metric('frequency_iqr', True, "substraction")

        self.skewness = Metric('skewness', True, "substraction")
        self.kurtosis = Metric('kurtosis', True, "substraction")

        self.val_pct_min = Metric('val_pct_min', False, "substraction")
        self.val_pct_max = Metric('val_pct_max', False, "substraction")
        self.val_pct_sd = Metric('val_pct_std', False, "substraction")
        self.val_pct_avg = Metric('val_pct_avg', False, "substraction")

        self.frequency_1qo = Metric('frequency_1qo', False, "substraction")
        self.frequency_2qo = Metric('frequency_2qo', False, "substraction")
        self.frequency_3qo = Metric('frequency_3qo', False, "substraction")
        self.frequency_4qo = Metric('frequency_4qo', False, "substraction")
        self.frequency_5qo = Metric('frequency_5qo', False, "substraction")
        self.frequency_6qo = Metric('frequency_6qo', False, "substraction")
        self.frequency_7qo = Metric('frequency_7qo', False, "substraction")

        self.metrics_list = [
            self.frequency_avg, self.frequency_min, self.frequency_max, self.frequency_sd, self.frequency_iqr,
            self.skewness, self.kurtosis,
            self.val_pct_min, self.val_pct_max, self.val_pct_sd, self.val_pct_avg,
            self.frequency_1qo, self.frequency_2qo, self.frequency_3qo, self.frequency_4qo, self.frequency_5qo,
            self.frequency_6qo, self.frequency_7qo,
        ]

    def build_query(self, column, table):
        return f"""
            SELECT
                AVG(count) AS {self.frequency_avg.name},
                MIN(count) AS {self.frequency_min.name},
                MAX(count) AS {self.frequency_max.name},
                STDDEV_POP(count) AS {self.frequency_sd.name},
                (quantile_disc(count, 0.75) - quantile_disc(count, 0.25)) AS {self.frequency_iqr.name},
                SKEWNESS(count) AS {self.skewness.name},
                KURTOSIS(count) AS {self.kurtosis.name},
                quantile_disc(count, 0.125) AS frequency_1qo,
                quantile_disc(count, 0.25) AS frequency_2qo,
                quantile_disc(count, 0.375) AS frequency_3qo,
                quantile_disc(count, 0.5) AS frequency_4qo,
                quantile_disc(count, 0.625) AS frequency_5qo,
                quantile_disc(count, 0.75) AS frequency_6qo,
                quantile_disc(count, 0.875) AS frequency_7qo
            FROM (
                SELECT COUNT("{column}") AS count
                FROM "{table}"
                WHERE "{column}" IS NOT NULL
                GROUP BY "{column}"
            )
        """

    def empty_result(self) -> dict:
        """All values are non-null-free (or column is entirely null): the
        inner GROUP BY produces zero groups, so every aggregate is NULL and
        the normal division-by-count_rows path below would blow up."""
        return {m.name: 0 for m in self.metrics_list}

    def process_result(self, result, count_rows):
        for key in (self.skewness.name, self.kurtosis.name):
            result[key] = _nan_to_zero(result[key])

        for key in (
            self.frequency_iqr.name,
            self.frequency_1qo.name, self.frequency_2qo.name, self.frequency_3qo.name, self.frequency_4qo.name,
            self.frequency_5qo.name, self.frequency_6qo.name, self.frequency_7qo.name,
        ):
            result[key] = result[key] / count_rows if count_rows > 0 else 0

        result[self.val_pct_min.name] = result[self.frequency_min.name] / count_rows if count_rows > 0 else 0
        result[self.val_pct_max.name] = result[self.frequency_max.name] / count_rows if count_rows > 0 else 0
        result[self.val_pct_sd.name] = result[self.frequency_sd.name] / count_rows if count_rows > 0 else 0
        result[self.val_pct_avg.name] = result[self.frequency_avg.name] / count_rows if count_rows > 0 else 0

        return result


class CommonValuesMetrics:
    def __init__(self):
        self.freq_word_containment = Metric('freq_word_containment', False, "containment")
        self.freq_word_soundex_containment = Metric('freq_word_soundex_containment', False, "containment")

        self.metrics_list = [self.freq_word_containment, self.freq_word_soundex_containment]

    def build_query(self, column, table):
        return f"""
            SELECT "{column}", COUNT("{column}") as count
            FROM "{table}"
            WHERE "{column}" IS NOT NULL
            GROUP BY "{column}"
            ORDER BY count DESC, "{column}" ASC
            LIMIT 10
        """

    def process_result(self, common_values):
        result = {self.freq_word_containment.name: [row[0] for row in common_values]}
        result[self.freq_word_soundex_containment.name] = [
            jellyfish.soundex(str(val)) if val is not None else None
            for val in result[self.freq_word_containment.name]
        ]
        return result


class LengthMetrics:
    def __init__(self):
        self.len_max_word = Metric('len_max_word', True, "substraction")
        self.len_min_word = Metric('len_min_word', True, "substraction")
        self.len_avg_word = Metric('len_avg_word', True, "substraction")
        self.len_max_full = Metric('len_max_full', True, "substraction")
        self.len_min_full = Metric('len_min_full', True, "substraction")
        self.len_avg_full = Metric('len_avg_full', True, "substraction")

        self.metrics_list = [
            self.len_max_word, self.len_min_word, self.len_avg_word,
            self.len_max_full, self.len_min_full, self.len_avg_full,
        ]

    def build_query(self, column, table):
        return f"""
            SELECT
                MAX(max_string_length) AS {self.len_max_word.name},
                MIN(min_string_length) AS {self.len_min_word.name},
                AVG(avg_string_length) AS {self.len_avg_word.name},
                MAX(full_length) AS {self.len_max_full.name},
                MIN(full_length) AS {self.len_min_full.name},
                AVG(full_length) AS {self.len_avg_full.name}
            FROM (
                SELECT
                    list_aggregate(list_transform(str_split(CAST("{column}" AS VARCHAR), ' '), s -> LENGTH(s)), 'max') AS max_string_length,
                    list_aggregate(list_transform(str_split(CAST("{column}" AS VARCHAR), ' '), s -> LENGTH(s)), 'min') AS min_string_length,
                    list_aggregate(list_transform(str_split(CAST("{column}" AS VARCHAR), ' '), s -> LENGTH(s)), 'avg') AS avg_string_length,
                    LENGTH(CAST("{column}" AS VARCHAR)) AS full_length
                FROM "{table}"
                WHERE "{column}" IS NOT NULL
            )
        """

    def process_result(self, result):
        return result


class WordCountMetrics:
    def __init__(self):
        self.number_words = Metric('number_words', True, "substraction")
        self.words_cnt_max = Metric('words_cnt_max', True, "substraction")
        self.words_cnt_min = Metric('words_cnt_min', True, "substraction")
        self.words_cnt_avg = Metric('words_cnt_avg', True, "substraction")
        self.words_cnt_sd = Metric('words_cnt_sd', True, "substraction")

        self.metrics_list = [
            self.number_words, self.words_cnt_max, self.words_cnt_min, self.words_cnt_avg, self.words_cnt_sd,
        ]

    def build_query(self, column, table):
        return f"""
            SELECT
                SUM(num_words) AS {self.number_words.name},
                MAX(num_words) AS {self.words_cnt_max.name},
                MIN(num_words) AS {self.words_cnt_min.name},
                AVG(num_words) AS {self.words_cnt_avg.name},
                STDDEV_POP(num_words) AS {self.words_cnt_sd.name}
            FROM (
                SELECT LENGTH(CAST("{column}" AS VARCHAR)) - LENGTH(REPLACE(CAST("{column}" AS VARCHAR), ' ', '')) + 1 AS num_words
                FROM "{table}"
                WHERE "{column}" IS NOT NULL
            )
        """

    def process_result(self, result):
        return result


class BoundaryMetrics:
    def __init__(self):
        self.first_word = Metric('first_word', False, "levenshtein")
        self.last_word = Metric('last_word', False, "levenshtein")

        self.metrics_list = [self.first_word, self.last_word]

    def build_query(self, column, table):
        return f"""
            SELECT
                MIN("{column}") AS first_word,
                MAX("{column}") AS last_word
            FROM "{table}"
            WHERE "{column}" IS NOT NULL
        """

    def process_result(self, result):
        return result


class ColumnFlagsMetrics:
    def __init__(self):
        self.is_empty = Metric('is_empty', False, "substraction")
        self.is_binary = Metric('is_binary', False, "substraction")

        self.metrics_list = [self.is_empty, self.is_binary]

    def build_query(self, column, table):
        return f"""
            SELECT COUNT(DISTINCT "{column}") AS count_distinct
            FROM "{table}"
            WHERE "{column}" IS NOT NULL
        """

    def process_result(self, count_distinct):
        return {
            self.is_empty.name: 1 if count_distinct[0] == 0 else 0,
            self.is_binary.name: 1 if count_distinct[0] == 2 else 0,
        }


class CoverageMetrics:
    def __init__(self):
        self.top_1_coverage = Metric('top_1_coverage', False, "substraction")
        self.top_5_coverage = Metric('top_5_coverage', False, "substraction")
        self.top_10_coverage = Metric('top_10_coverage', False, "substraction")

        self.metrics_list = [self.top_1_coverage, self.top_5_coverage, self.top_10_coverage]

    def build_query(self, column, table):
        return f"""
            WITH value_counts AS (
                SELECT
                    "{column}",
                    COUNT(*) AS freq
                FROM "{table}"
                WHERE "{column}" IS NOT NULL
                GROUP BY "{column}"
            ),
            total_count AS (
                SELECT COUNT(*) AS values_count
                FROM "{table}"
                WHERE "{column}" IS NOT NULL
            ),
            ranked_values AS (
                SELECT
                    freq,
                    ROW_NUMBER() OVER (ORDER BY freq DESC) AS rank
                FROM value_counts
            )
            SELECT
                tc.values_count,
                COALESCE(SUM(CASE WHEN rv.rank <= 1 THEN rv.freq ELSE 0 END), 0) AS top_1_sum,
                COALESCE(SUM(CASE WHEN rv.rank <= 5 THEN rv.freq ELSE 0 END), 0) AS top_5_sum,
                COALESCE(SUM(CASE WHEN rv.rank <= 10 THEN rv.freq ELSE 0 END), 0) AS top_10_sum
            FROM total_count tc
            LEFT JOIN ranked_values rv ON 1=1
            GROUP BY tc.values_count
        """

    def process_result(self, result):
        values_count = result.get('values_count', 0)
        return {
            'top_1_coverage': result['top_1_sum'] / values_count if values_count > 0 else 0.0,
            'top_5_coverage': result['top_5_sum'] / values_count if values_count > 0 else 0.0,
            'top_10_coverage': result['top_10_sum'] / values_count if values_count > 0 else 0.0,
        }


class NumericalMetrics:
    def __init__(self):
        self.percentage_negative = Metric('percentage_negative', False, "substraction")
        self.number_zero = Metric('number_zero', False, "substraction")
        self.outliers_below_iqr = Metric('outliers_below_iqr', True, "substraction")
        self.outliers_above_iqr = Metric('outliers_above_iqr', True, "substraction")
        self.total_outliers = Metric('total_outliers', True, "substraction")
        self.percentage_outliers = Metric('percentage_outliers', False, "substraction")

        self.metrics_list = [
            self.percentage_negative, self.number_zero,
            self.outliers_below_iqr, self.outliers_above_iqr, self.total_outliers, self.percentage_outliers,
        ]

    def build_query(self, column, table):
        return f"""
            WITH stats AS (
                SELECT
                    COUNT(*) AS values_count,
                    SUM(CASE WHEN "{column}" < 0 THEN 1 ELSE 0 END) AS negative_count,
                    SUM(CASE WHEN "{column}" = 0 THEN 1 ELSE 0 END) AS zero_count
                FROM "{table}"
            ),
            percentiles AS (
                SELECT
                    percentile_cont(0.25) WITHIN GROUP (ORDER BY "{column}") AS q1,
                    percentile_cont(0.75) WITHIN GROUP (ORDER BY "{column}") AS q3
                FROM "{table}"
            ),
            bounds AS (
                SELECT
                    q1, q3, (q3 - q1) AS iqr,
                    q1 - 1.5 * (q3 - q1) AS lower_bound,
                    q3 + 1.5 * (q3 - q1) AS upper_bound
                FROM percentiles
            ),
            outlier_counts AS (
                SELECT
                    COUNT(*) AS total_outliers,
                    SUM(CASE WHEN "{column}" < lower_bound THEN 1 ELSE 0 END) AS outliers_below_iqr,
                    SUM(CASE WHEN "{column}" > upper_bound THEN 1 ELSE 0 END) AS outliers_above_iqr
                FROM "{table}", bounds
            )
            SELECT
                s.values_count, s.negative_count, s.zero_count,
                o.outliers_below_iqr, o.outliers_above_iqr, o.total_outliers
            FROM stats s, outlier_counts o
        """

    def process_result(self, result):
        values_count = result.get('values_count', 0)
        total_outliers = result.get('total_outliers', 0)
        outliers_below = result.get('outliers_below_iqr', 0) or 0
        outliers_above = result.get('outliers_above_iqr', 0) or 0

        return {
            'values_count': values_count,
            'percentage_negative': (result['negative_count'] or 0) / values_count if values_count > 0 else 0.0,
            'number_zero': (result['zero_count'] or 0) / values_count if values_count > 0 else 0.0,
            'outliers_below_iqr': outliers_below,
            'outliers_above_iqr': outliers_above,
            'total_outliers': total_outliers,
            'percentage_outliers': (outliers_below + outliers_above) / total_outliers if total_outliers > 0 else 0.0,
        }
