"""Semantic validation of a candidate statement (P4-F07).

The guard (P4-F05) answers "is this *safe*". This module answers "is this
*meaningful* against the authorized slice": do the columns exist, is an aggregate
applied to something numeric, is everything that is not aggregated actually
grouped.

The bias is deliberately towards silence. A false refusal blocks a legitimate
question, and the database is a better judge than a partial model of it; so a check
only fires when it is *sure*:

* a qualified column is checked only when its qualifier resolves to a dataset in
  the slice (an unknown qualifier is the guard's business, not this module's);
* an unqualified column is checked only in a statement that references exactly one
  dataset and defines no CTE, because otherwise the name may belong to any of them;
* an aggregate type error needs a known column type — an untyped column passes;
* grouping is only reasoned about when the select list mixes aggregates with plain
  columns, which no dialect accepts ungrouped.

Refusals are stable codes (``unknown_column``, ``non_numeric_aggregate``,
``ungrouped_column``, ``unknown_dataset``) so the audit trail stays content-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from xp_prompt import sql_guard
from xp_prompt.schema_slice import SchemaSlice

#: Type substrings that make a column usable inside SUM/AVG.
NUMERIC_TYPE_HINTS: tuple[str, ...] = (
    "INT",
    "SERIAL",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "DECIMAL",
    "NUMERIC",
    "NUMBER",
    "MONEY",
    "DEC",
    "FIXED",
)

AGGREGATE_NAMES: frozenset[str] = frozenset({"SUM", "AVG", "STDDEV", "VARIANCE"})

MAX_ISSUES = 5
MAX_DETAIL_CHARS = 200

UNKNOWN_COLUMN = "unknown_column"
NON_NUMERIC_AGGREGATE = "non_numeric_aggregate"
UNGROUPED_COLUMN = "ungrouped_column"
UNKNOWN_DATASET = "unknown_dataset"


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    detail: str

    def to_public_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class SemanticsResult:
    issues: tuple[Issue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def code(self) -> str:
        return self.issues[0].code if self.issues else "ok"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "issues": [issue.to_public_dict() for issue in self.issues],
        }

    def message(self) -> str:
        if not self.issues:
            return "The statement matches the authorized schema."
        return "; ".join(issue.detail for issue in self.issues)


class SemanticsError(RuntimeError):
    """The statement could not be examined at all (not the same as "invalid")."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate(
    sql: str, *, schema: SchemaSlice, dialect: str = "postgres"
) -> SemanticsResult:
    """Check one statement against the slice. Never raises for a *user* mistake."""
    try:
        statement = sql_guard.parse_query(sql, dialect)
    except sql_guard.SqlPolicyError as exc:
        raise SemanticsError(exc.code, str(exc)) from exc

    by_reference = _datasets_by_reference(schema)
    cte_names = {
        cte.alias_or_name.lower()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }
    issues: list[Issue] = []
    for select in _selects(statement):
        issues.extend(_check_select(select, by_reference, cte_names))
        if len(issues) >= MAX_ISSUES:
            break
    return SemanticsResult(issues=tuple(issues[:MAX_ISSUES]))


def _datasets_by_reference(schema: SchemaSlice) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for dataset in schema.datasets:
        for form in dataset.reference_forms():
            mapping.setdefault(form, dataset)
    return mapping


def _selects(statement: exp.Expression) -> list[exp.Select]:
    if isinstance(statement, exp.Select):
        return [statement]
    return [node for node in statement.find_all(exp.Select)]


def _check_select(
    select: exp.Select, by_reference: dict[str, Any], cte_names: set[str]
) -> list[Issue]:
    scoped, unknown = _scoped_datasets(select, by_reference, cte_names)
    issues: list[Issue] = []
    if unknown:
        issues.append(
            Issue(UNKNOWN_DATASET, f"Unknown dataset in the query: {unknown}.")
        )

    issues.extend(_check_columns(select, scoped, cte_names))
    issues.extend(_check_aggregates(select, scoped))
    issues.extend(_check_grouping(select, scoped))
    return issues


def _scoped_datasets(
    select: exp.Select, by_reference: dict[str, Any], cte_names: set[str]
) -> tuple[dict[str, Any], str]:
    """alias -> dataset for this select's FROM/JOIN clauses, plus an unknown name."""
    scoped: dict[str, Any] = {}
    unknown = ""
    for table in select.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue
        reference = ".".join(
            str(part).lower() for part in (table.catalog, table.db, name) if part
        )
        dataset = by_reference.get(reference) or by_reference.get(name)
        if dataset is None:
            unknown = unknown or table.name
            continue
        scoped[(table.alias_or_name or name).lower()] = dataset
        scoped.setdefault(name, dataset)
    return scoped, unknown


def _check_columns(
    select: exp.Select, scoped: dict[str, Any], cte_names: set[str]
) -> list[Issue]:
    issues: list[Issue] = []
    single = len({id(dataset) for dataset in scoped.values()}) == 1
    only_dataset = next(iter(scoped.values())) if single and scoped else None
    # A select alias is a legal reference in GROUP BY / ORDER BY / HAVING, so a bare
    # column that matches one is not a missing column.
    aliases = {
        (expression.alias or "").lower()
        for expression in select.expressions
        if isinstance(expression, exp.Alias) and expression.alias
    }
    for column in select.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        name = (column.name or "").lower()
        if not name:
            continue
        qualifier = (column.table or "").lower()
        if qualifier:
            dataset = scoped.get(qualifier)
            if dataset is None:
                # A qualifier we cannot resolve may be a CTE or a subquery alias:
                # not this module's call.
                continue
            if not _has_column(dataset, name):
                issues.append(
                    Issue(
                        UNKNOWN_COLUMN,
                        f"Column '{name}' does not exist in dataset "
                        f"'{dataset.name}'.",
                    )
                )
        elif name in aliases:
            continue
        elif only_dataset is not None and not cte_names:
            if not _has_column(only_dataset, name):
                issues.append(
                    Issue(
                        UNKNOWN_COLUMN,
                        f"Column '{name}' does not exist in dataset "
                        f"'{only_dataset.name}'.",
                    )
                )
        if len(issues) >= MAX_ISSUES:
            break
    return issues


def _check_aggregates(select: exp.Select, scoped: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    for node in select.find_all(exp.Func):
        if _function_name(node) not in AGGREGATE_NAMES:
            continue
        argument = node.this
        if not isinstance(argument, exp.Column):
            continue
        dataset = _dataset_for_column(argument, scoped)
        if dataset is None:
            continue
        column = _find_column(dataset, (argument.name or "").lower())
        if column is None or not column.type:
            continue
        if not _is_numeric(column.type):
            name = _function_name(node).lower()
            issues.append(
                Issue(
                    NON_NUMERIC_AGGREGATE,
                    f"{name.upper()}() needs a numeric column, but "
                    f"'{column.name}' is {column.type}.",
                )
            )
    return issues


def _check_grouping(select: exp.Select, scoped: dict[str, Any]) -> list[Issue]:
    """Refuse a select list that mixes aggregates with ungrouped plain columns."""
    if not scoped:
        return []
    selected = list(select.expressions)
    if any(_is_star(expression) for expression in selected):
        return []
    aggregated = [expression for expression in selected if _has_aggregate(expression)]
    if not aggregated:
        return []
    plain = [
        expression
        for expression in selected
        if not _has_aggregate(expression) and _columns_in(expression)
    ]
    if not plain:
        return []

    grouped = _grouped_expressions(select, selected)
    issues: list[Issue] = []
    for expression in plain:
        rendered = _normalise(_underlying(expression))
        if rendered in grouped:
            continue
        issues.append(
            Issue(
                UNGROUPED_COLUMN,
                f"'{rendered}' is selected with an aggregate but is not grouped "
                "or aggregated.",
            )
        )
        if len(issues) >= MAX_ISSUES:
            break
    return issues


def _grouped_expressions(select: exp.Select, selected: list[exp.Expression]) -> set[str]:
    """What GROUP BY actually covers: expressions, ordinals, and select aliases."""
    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        return set()
    aliases = {
        (expression.alias or "").lower(): _normalise(_underlying(expression))
        for expression in selected
        if isinstance(expression, exp.Alias) and expression.alias
    }
    grouped: set[str] = set()
    for entry in group.expressions:
        if isinstance(entry, exp.Literal) and not entry.is_string:
            try:
                position = int(entry.this)
            except (TypeError, ValueError):
                continue
            if 1 <= position <= len(selected):
                grouped.add(_normalise(_underlying(selected[position - 1])))
            continue
        name = (getattr(entry, "name", "") or "").lower()
        if isinstance(entry, exp.Column) and not entry.table and name in aliases:
            # "GROUP BY <alias>" covers the expression the alias names.
            grouped.add(aliases[name])
            continue
        grouped.add(_normalise(entry))
    return grouped


def _underlying(expression: exp.Expression) -> exp.Expression:
    return expression.this if isinstance(expression, exp.Alias) else expression


def _dataset_for_column(column: exp.Column, scoped: dict[str, Any]) -> Any:
    qualifier = (column.table or "").lower()
    if qualifier:
        return scoped.get(qualifier)
    unique = {id(dataset): dataset for dataset in scoped.values()}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _has_column(dataset: Any, name: str) -> bool:
    return _find_column(dataset, name) is not None


def _find_column(dataset: Any, name: str) -> Any:
    for column in dataset.columns:
        if column.name.lower() == name:
            return column
    return None


def _is_numeric(type_name: str) -> bool:
    upper = str(type_name or "").upper()
    return any(hint in upper for hint in NUMERIC_TYPE_HINTS)


def _function_name(node: exp.Func) -> str:
    try:
        name = node.sql_name()
    except (AttributeError, ValueError):  # pragma: no cover - defensive
        name = ""
    return (name or "").upper()


def _has_aggregate(expression: exp.Expression | None) -> bool:
    if expression is None:
        return False
    for node in expression.walk():
        if isinstance(node, exp.AggFunc):
            return True
    return False


def _columns_in(expression: exp.Expression) -> list[exp.Column]:
    return [node for node in expression.walk() if isinstance(node, exp.Column)]


def _is_star(expression: exp.Expression) -> bool:
    if isinstance(expression, exp.Star):
        return True
    return isinstance(expression, exp.Column) and isinstance(expression.this, exp.Star)


def _normalise(expression: exp.Expression | None) -> str:
    if expression is None:  # pragma: no cover - defensive
        return ""
    return " ".join(expression.sql(dialect="postgres").lower().split())[
        :MAX_DETAIL_CHARS
    ]
