"""
Shared helper for building DuckDB SQL safely from arbitrary column/table
names. See data_loader._read_raw_cached for the BOM-related bug (a column
name ending up with a literal embedded quote character) this exists to
stop from recurring anywhere new SQL gets built.

column_profiler/profiler.py uses an equivalent but differently-shaped
pattern (pre-escaping a value that gets substituted into metrics.py query
templates which already have the literal wrapping quotes baked in) - that
code was left as-is rather than retrofitted to this helper, since it's
already correct and touching every query template for a cosmetic DRY win
wasn't worth the risk. This helper is for code that builds SQL identifiers
directly, like app/join_builder.py.
"""
from __future__ import annotations


def quote_ident(name: str) -> str:
    """Wraps name as a valid, escaped DuckDB quoted identifier - doubling
    any embedded '"' per SQL's own escaping convention."""
    return '"' + name.replace('"', '""') + '"'
