from __future__ import annotations

from typing import Any


NAVY = "#1c2545"
ORANGE = "#e04403"
TEAL = "#007d6d"
MUTED = "#64748b"
GRID = "#dce4ef"


def _base() -> dict[str, Any]:
    return {
        "animationDuration": 350,
        "textStyle": {"fontFamily": "Segoe UI, sans-serif", "color": NAVY},
        "tooltip": {
            "trigger": "axis",
            "backgroundColor": "rgba(255,255,255,.98)",
            "borderColor": GRID,
            "textStyle": {"color": NAVY},
        },
        "grid": {"left": 44, "right": 22, "top": 46, "bottom": 38},
    }


def activity_options(rows: list[dict[str, Any]]) -> dict[str, Any]:
    options = _base()
    options.update(
        {
            "legend": {
                "top": 4,
                "right": 4,
                "textStyle": {"color": MUTED},
                "data": ["Allocations", "Checklist jobs"],
            },
            "xAxis": {
                "type": "category",
                "boundaryGap": False,
                "data": [row["activity_date"] for row in rows],
                "axisLabel": {"color": MUTED, "hideOverlap": True},
                "axisLine": {"lineStyle": {"color": GRID}},
            },
            "yAxis": {
                "type": "value",
                "minInterval": 1,
                "axisLabel": {"color": MUTED},
                "splitLine": {"lineStyle": {"color": GRID, "type": "dashed"}},
            },
            "series": [
                {
                    "name": "Allocations",
                    "type": "line",
                    "smooth": True,
                    "showSymbol": False,
                    "lineStyle": {"width": 3, "color": ORANGE},
                    "itemStyle": {"color": ORANGE},
                    "areaStyle": {"color": "rgba(224,68,3,.10)"},
                    "data": [row["allocations"] for row in rows],
                },
                {
                    "name": "Checklist jobs",
                    "type": "line",
                    "smooth": True,
                    "showSymbol": False,
                    "lineStyle": {"width": 3, "color": TEAL},
                    "itemStyle": {"color": TEAL},
                    "areaStyle": {"color": "rgba(0,125,109,.08)"},
                    "data": [row["checklist_jobs"] for row in rows],
                },
            ],
        }
    )
    return options

def coverage_options(rows: list[dict[str, Any]]) -> dict[str, Any]:
    options = _base()
    options["grid"] = {"left": 150, "right": 24, "top": 12, "bottom": 32}
    options.update(
        {
            "tooltip": {
                **options["tooltip"],
                "trigger": "item",
                "formatter": "{b}<br/>{c} checklist jobs",
            },
            "xAxis": {
                "type": "value",
                "minInterval": 1,
                "axisLabel": {"color": MUTED},
                "splitLine": {"lineStyle": {"color": GRID, "type": "dashed"}},
            },
            "yAxis": {
                "type": "category",
                "inverse": True,
                "data": [row["canonical_name"] for row in rows],
                "axisLabel": {
                    "color": MUTED,
                    "width": 135,
                    "overflow": "truncate",
                },
                "axisTick": {"show": False},
                "axisLine": {"show": False},
            },
            "series": [
                {
                    "type": "bar",
                    "barMaxWidth": 18,
                    "data": [row["job_rows"] for row in rows],
                    "itemStyle": {
                        "color": ORANGE,
                        "borderRadius": [0, 5, 5, 0],
                    },
                }
            ],
        }
    )
    return options


def grouped_bar_options(
    rows: list[dict[str, Any]],
    *,
    category_key: str,
    series: list[tuple[str, str, str]],
    horizontal: bool = False,
    show_labels: bool = False,
    value_suffix: str = "",
) -> dict[str, Any]:
    options = _base()
    categories = [str(row.get(category_key) or "Unknown") for row in rows]
    value_axis = {
        "type": "value",
        "name": "Tonnes" if value_suffix.strip() == "t" else "",
        "axisLabel": {
            "color": MUTED,
            "formatter": f"{{value}} {value_suffix}".rstrip(),
        },
        "splitLine": {"lineStyle": {"color": GRID, "type": "dashed"}},
    }
    category_axis = {
        "type": "category",
        "data": categories,
        "axisLabel": {
            "color": MUTED,
            "hideOverlap": True,
            "interval": 0 if len(categories) <= 12 else "auto",
            "rotate": 28 if not horizontal and len(categories) > 8 else 0,
        },
        "axisLine": {"lineStyle": {"color": GRID}},
    }
    options["grid"] = {
        "left": 130 if horizontal else 58,
        "right": 88 if horizontal and show_labels else 24,
        "top": 52,
        "bottom": 78 if not horizontal and len(categories) > 8 else 44,
    }
    options.update(
        {
            "legend": {
                "top": 4,
                "right": 4,
                "textStyle": {"color": MUTED},
                "data": [name for name, _key, _color in series],
            },
            "xAxis": value_axis if horizontal else category_axis,
            "yAxis": category_axis if horizontal else value_axis,
            "series": [
                {
                    "name": name,
                    "type": "bar",
                    "barMaxWidth": 24,
                    "itemStyle": {
                        "color": color,
                        "borderRadius": (
                            [0, 4, 4, 0] if horizontal else [4, 4, 0, 0]
                        ),
                    },
                    "label": {
                        "show": show_labels,
                        "position": "right" if horizontal else "top",
                        "color": NAVY,
                        "fontSize": 10,
                        "formatter": f"{{c}} {value_suffix}".rstrip(),
                    },
                    "data": [float(row.get(key) or 0) for row in rows],
                }
                for name, key, color in series
            ],
        }
    )
    return options


def route_heatmap_options(
    rows: list[dict[str, Any]],
    *,
    value_key: str = "tonnes",
) -> dict[str, Any]:
    origins = sorted({str(row.get("origin") or "Unknown") for row in rows})
    destinations = sorted(
        {str(row.get("destination") or "Unknown") for row in rows}
    )
    origin_indexes = {value: index for index, value in enumerate(origins)}
    destination_indexes = {
        value: index for index, value in enumerate(destinations)
    }
    values = [
        [
            destination_indexes[str(row.get("destination") or "Unknown")],
            origin_indexes[str(row.get("origin") or "Unknown")],
            float(row.get(value_key) or 0),
        ]
        for row in rows
    ]
    maximum = max((value[2] for value in values), default=0)
    options = _base()
    options["grid"] = {"left": 180, "right": 90, "top": 24, "bottom": 120}
    options.update(
        {
            "tooltip": {
                **options["tooltip"],
                "trigger": "item",
            },
            "xAxis": {
                "type": "category",
                "name": "Destination",
                "data": destinations,
                "axisLabel": {
                    "color": MUTED,
                    "interval": 0,
                    "rotate": 28 if len(destinations) > 3 else 0,
                    "width": 150,
                    "overflow": "truncate",
                },
                "splitArea": {"show": True},
            },
            "yAxis": {
                "type": "category",
                "name": "Origin",
                "data": origins,
                "axisLabel": {
                    "color": MUTED,
                    "width": 165,
                    "overflow": "truncate",
                },
                "splitArea": {"show": True},
            },
            "visualMap": {
                "min": 0,
                "max": maximum or 1,
                "calculable": True,
                "orient": "vertical",
                "right": 4,
                "top": "middle",
                "inRange": {"color": ["#e7f4f1", TEAL, NAVY]},
                "textStyle": {"color": MUTED},
            },
            "series": [
                {
                    "type": "heatmap",
                    "data": values,
                    "label": {"show": len(values) <= 20},
                    "emphasis": {
                        "itemStyle": {
                            "shadowBlur": 8,
                            "shadowColor": "rgba(28,37,69,.25)",
                        }
                    },
                }
            ],
        }
    )
    return options


def donut_options(
    rows: list[dict[str, Any]],
    *,
    category_key: str,
    value_key: str,
) -> dict[str, Any]:
    options = _base()
    options["grid"] = {}
    options.update(
        {
            "tooltip": {**options["tooltip"], "trigger": "item"},
            "legend": {
                "type": "scroll",
                "bottom": 0,
                "textStyle": {"color": MUTED},
            },
            "series": [
                {
                    "type": "pie",
                    "radius": ["48%", "72%"],
                    "center": ["50%", "44%"],
                    "label": {"show": False},
                    "emphasis": {"label": {"show": True, "fontWeight": "bold"}},
                    "data": [
                        {
                            "name": str(row.get(category_key) or "Unknown"),
                            "value": float(row.get(value_key) or 0),
                        }
                        for row in rows
                    ],
                }
            ],
        }
    )
    return options


def movement_trend_options(
    rows: list[dict[str, Any]],
    *,
    show_labels: bool = False,
    value_suffix: str = "",
) -> dict[str, Any]:
    options = _base()
    options.update(
        {
            "legend": {
                "top": 4,
                "right": 4,
                "textStyle": {"color": MUTED},
                "data": ["Loaded", "Offloaded"],
            },
            "xAxis": {
                "type": "category",
                "data": [str(row.get("activity_date") or "") for row in rows],
                "axisLabel": {"color": MUTED, "hideOverlap": True},
                "axisLine": {"lineStyle": {"color": GRID}},
            },
            "yAxis": {
                "type": "value",
                "name": "Tonnes" if value_suffix.strip() == "t" else "",
                "axisLabel": {
                    "color": MUTED,
                    "formatter": f"{{value}} {value_suffix}".rstrip(),
                },
                "splitLine": {"lineStyle": {"color": GRID, "type": "dashed"}},
            },
            "series": [
                {
                    "name": "Loaded",
                    "type": "line",
                    "smooth": True,
                    "showSymbol": False,
                    "label": {
                        "show": show_labels,
                        "position": "top",
                        "formatter": f"{{c}} {value_suffix}".rstrip(),
                    },
                    "lineStyle": {"width": 3, "color": ORANGE},
                    "itemStyle": {"color": ORANGE},
                    "data": [float(row.get("loaded_tonnes") or 0) for row in rows],
                },
                {
                    "name": "Offloaded",
                    "type": "line",
                    "smooth": True,
                    "showSymbol": False,
                    "label": {
                        "show": show_labels,
                        "position": "bottom",
                        "formatter": f"{{c}} {value_suffix}".rstrip(),
                    },
                    "lineStyle": {"width": 3, "color": TEAL},
                    "itemStyle": {"color": TEAL},
                    "data": [float(row.get("offloaded_tonnes") or 0) for row in rows],
                },
            ],
        }
    )
    return options
