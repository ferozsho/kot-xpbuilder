"""What this deployment can actually render, and what each visualization accepts (P7-F01).

Two different questions live here, and they have different authorities:

* **"Is this viz type available?"** — the deployment answers. Superset's backend registry
  (``superset.viz.viz_types``) knows the legacy types; the modern ECharts types exist in
  the *frontend* build, which the API process cannot enumerate. So the proxy reports what
  the deployment declares, and this module treats that report as the authority — the
  service never recommends a viz type it was not told about.
* **"Does this visualization accept these controls?"** — this module answers, from
  :data:`VIZ_CONTRACTS`. That is the guard behind P7-F04 ("AI cannot invent unsupported
  controls") and P7-F05 ("schema-valid configuration"): a proposal is only valid when
  every key it carries is one the target viz type actually reads.

The contracts are deliberately small: they describe the subset of each chart's form data
this feature *emits*, not the whole Explore control panel. A viz type outside the
contracts is still *available* — it just cannot be recommended, because nothing here
knows how to configure it safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_VIZ_TYPES = 200
MAX_VIZ_NAME_CHARS = 80
MAX_VIZ_TYPE_CHARS = 64
MAX_FORM_KEYS = 40

#: Keys every chart may carry, whatever its type.
COMMON_KEYS: frozenset[str] = frozenset(
    {
        "viz_type",
        "datasource",
        "adhoc_filters",
        "row_limit",
        "time_range",
        "granularity_sqla",
    }
)


@dataclass(frozen=True, slots=True)
class VizContract:
    """The controls one visualization reads, as far as this feature is concerned."""

    viz_type: str
    label: str
    timeseries: bool
    requires: frozenset[str]
    allows: frozenset[str]

    def accepts(self, form_data: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
        """``(ok, unsupported_keys)`` — the only honest answer to "can it read this?"."""
        keys = {str(key) for key in form_data}
        unknown = tuple(sorted(key for key in keys if key not in self.allows))
        missing = tuple(sorted(key for key in self.requires if key not in keys))
        return (not unknown and not missing, unknown + tuple(f"missing:{m}" for m in missing))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "viz_type": self.viz_type,
            "label": self.label,
            "timeseries": self.timeseries,
            "requires": sorted(self.requires),
            "allows": sorted(self.allows),
        }


_TIME_KEYS = frozenset({"x_axis", "metrics", "groupby", "time_grain_sqla", "y_axis_format", "show_legend"})
_CATEGORY_KEYS = frozenset({"x_axis", "metrics", "groupby", "y_axis_format", "show_legend"})

#: The contract per visualization this feature knows how to configure.
VIZ_CONTRACTS: dict[str, VizContract] = {
    contract.viz_type: contract
    for contract in (
        VizContract(
            viz_type="echarts_timeseries_line",
            label="Line chart",
            timeseries=True,
            requires=frozenset({"x_axis", "metrics"}),
            allows=COMMON_KEYS | _TIME_KEYS,
        ),
        VizContract(
            viz_type="echarts_timeseries_bar",
            label="Bar chart",
            timeseries=False,
            requires=frozenset({"x_axis", "metrics"}),
            allows=COMMON_KEYS | _TIME_KEYS | _CATEGORY_KEYS,
        ),
        VizContract(
            viz_type="echarts_timeseries_area",
            label="Area chart",
            timeseries=True,
            requires=frozenset({"x_axis", "metrics"}),
            allows=COMMON_KEYS | _TIME_KEYS,
        ),
        VizContract(
            viz_type="echarts_timeseries_scatter",
            label="Scatter plot",
            timeseries=True,
            requires=frozenset({"x_axis", "metrics"}),
            allows=COMMON_KEYS | _TIME_KEYS,
        ),
        VizContract(
            viz_type="pie",
            label="Pie chart",
            timeseries=False,
            requires=frozenset({"groupby", "metric"}),
            allows=COMMON_KEYS | frozenset({"groupby", "metric", "y_axis_format", "show_legend"}),
        ),
        VizContract(
            viz_type="heatmap_v2",
            label="Heatmap",
            timeseries=False,
            requires=frozenset({"x_axis", "groupby", "metric"}),
            allows=COMMON_KEYS | frozenset({"x_axis", "groupby", "metric", "y_axis_format"}),
        ),
        VizContract(
            viz_type="big_number_total",
            label="Big number",
            timeseries=False,
            requires=frozenset({"metric"}),
            allows=COMMON_KEYS | frozenset({"metric", "y_axis_format", "time_grain_sqla"}),
        ),
        VizContract(
            viz_type="table",
            label="Table",
            timeseries=False,
            requires=frozenset(),
            allows=COMMON_KEYS | frozenset({"groupby", "metrics", "order_desc", "y_axis_format"}),
        ),
    )
}


class CatalogError(ValueError):
    """A refused catalog payload; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Catalog:
    """The deployment's available viz types, as reported by the proxy."""

    available: frozenset[str] = frozenset()
    labels: dict[str, str] = field(default_factory=dict)
    #: Reported by the proxy when the deployment denies a type outright.
    denied: frozenset[str] = frozenset()

    @classmethod
    def from_payload(cls, payload: Any) -> "Catalog":
        """Normalise what the proxy reported; an empty report is not an error.

        An empty catalog means "no chart may be recommended" — which is the safe
        answer, and better than falling back to a hard-coded list that may not exist
        in this build.
        """
        body = payload if isinstance(payload, dict) else {}
        denied = _names(body.get("denylist"))
        entries = body.get("viz_types")
        available: set[str] = set()
        labels: dict[str, str] = {}
        for entry in entries if isinstance(entries, (list, tuple)) else ():
            if isinstance(entry, str):
                name, label = entry, ""
            elif isinstance(entry, dict):
                name = str(entry.get("viz_type") or "").strip()
                label = str(entry.get("name") or "").strip()
            else:
                continue
            clean = _viz_type(name)
            if not clean or clean in denied:
                continue
            available.add(clean)
            if label:
                labels[clean] = label[:MAX_VIZ_NAME_CHARS]
            if len(available) >= MAX_VIZ_TYPES:
                break
        return cls(available=frozenset(available), labels=labels, denied=frozenset(denied))

    def has(self, viz_type: str) -> bool:
        return _viz_type(viz_type) in self.available

    def contract(self, viz_type: str) -> VizContract | None:
        """The contract for an available viz type, or ``None`` if it is unusable."""
        clean = _viz_type(viz_type)
        if not clean or clean not in self.available:
            return None
        return VIZ_CONTRACTS.get(clean)

    def supported(self) -> list[VizContract]:
        """Contracts that are both known to this feature and available here."""
        return [
            contract
            for contract in VIZ_CONTRACTS.values()
            if contract.viz_type in self.available
        ]

    def validate(self, viz_type: str, form_data: Any) -> tuple[bool, tuple[str, ...]]:
        """Is ``viz_type`` renderable here, and does it accept every key in ``form_data``?"""
        contract = self.contract(viz_type)
        if contract is None:
            return False, ("unavailable_viz_type",)
        body = form_data if isinstance(form_data, dict) else {}
        if len(body) > MAX_FORM_KEYS:
            return False, ("too_many_controls",)
        return contract.accepts(body)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "available": sorted(self.available),
            "denied": sorted(self.denied),
            "contracts": [contract.to_public_dict() for contract in self.supported()],
        }


def _viz_type(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    return text[:MAX_VIZ_TYPE_CHARS] if text else ""


def _names(value: Any) -> frozenset[str]:
    names = {_viz_type(str(entry)) for entry in value if entry} if isinstance(
        value, (list, tuple)
    ) else set()
    names.discard("")
    return frozenset(names)
