"""Deterministic CSP-compliant static HTML renderer for FinOps reports."""

import html
import re
import json
from datetime import datetime, timezone
from pathlib import Path


_CSP = """default-src 'none';
script-src 'self' 'nonce-{REPORT_NONCE}' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://unpkg.com https://esm.sh;
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com;
font-src 'self' data: https://fonts.gstatic.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com;
img-src 'self' data: blob: https:;
connect-src 'self';"""

_CHART_JS = (
    '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js" '
    'integrity="sha384-vsrfeLOOY6KuIYKDlmVH5UiBmgIdB1oEf7p01YgWHuqmOHfZr374+odEv96n9tNC" '
    'crossorigin="anonymous"></script>'
)

_TONES = {"neutral", "good", "warning", "critical"}
_TONE_LABELS = {"good": "Good", "warning": "Watch", "critical": "Action"}
_VALUE_FORMATS = {"currency", "percent", "number"}

# Fluent 2 / Azure palette. Foreground colors meet WCAG AA (>= 4.5:1) on white and on
# their tinted backgrounds; tests/test_report_renderer.py guards these pairs.
PALETTE = {
    "brand": "#0f6cbd",
    "brand_dark": "#0c3b5e",
    "text": "#242424",
    "muted": "#5c5c5c",
    "border": "#e0e0e0",
    "canvas": "#f5f5f5",
    "good": "#0e700e",
    "good_bg": "#f1faf1",
    "warning": "#8a3707",
    "warning_bg": "#fff9f5",
    "critical": "#b10e1c",
    "critical_bg": "#fdf3f4",
    "neutral": "#5c5c5c",
    "neutral_bg": "#f5f5f5",
    "info_bg": "#ebf3fc",
}

# Whole-cell status words rendered as badges. Text is kept verbatim; only the
# visual treatment changes, and color is never the only signal.
_BADGES = {
    "critical": {"fail", "failed", "over", "over budget", "forecast over", "critical",
                 "high", "sev0", "sev1", "breach"},
    "warning": {"warn", "warning", "at risk", "review", "verify first", "verify",
                "medium", "sev2", "partial", "needs review"},
    "good": {"pass", "passed", "on track", "complete", "validated", "ok", "healthy"},
    "neutral": {"unknown", "low", "sev3", "sev4", "unavailable", "n/a", "info"},
}
_BADGE_TONE = {word: tone for tone, words in _BADGES.items() for word in words}

_VISIBLE_ROWS = 15
_COLLAPSE_AFTER = 20

_NUMERIC_CELL = re.compile(r"^(?:[≥≤<>]\s?)?[-+]?[$€£]?[-+]?[\d.,]+\s?(%|[A-Z]{3}|AAU)?$")
_SNAKE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
_ARM_ID = re.compile(
    r"^/subscriptions/[^/\s]+/resourceGroups/(?P<rg>[^/\s]+)/providers/"
    r"(?P<type>[^/\s]+/[^/\s]+)(?:/[^/\s]+)*/(?P<name>[^/\s]+)$",
    re.IGNORECASE,
)


def _escape(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _list(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _required_text(model, field):
    value = str(model.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    return value


def _metric_items(model):
    items = []
    for index, metric in enumerate(_list(model.get("metrics"), "metrics")):
        if not isinstance(metric, dict):
            raise ValueError(f"metrics[{index}] must be an object")
        label = _required_text(metric, "label")
        value = _required_text(metric, "value")
        detail = str(metric.get("detail") or "")
        tone = str(metric.get("tone") or "neutral").lower()
        if tone not in _TONES:
            raise ValueError(f"metrics[{index}].tone is invalid")
        items.append((label, value, detail, tone))
    return items


def _metrics(items):
    if not items:
        return '<p class="empty">No headline metrics were produced.</p>'
    rendered = []
    for label, value, detail, tone in items:
        flag = (
            f'<span class="tone-label">{_TONE_LABELS[tone]}</span>' if tone in _TONE_LABELS else ""
        )
        rendered.append(
            f'<article class="metric {tone}"><div class="metric-label">{_escape(label)}{flag}</div>'
            f'<div class="metric-value">{_escape(value)}</div>'
            f'<div class="metric-detail">{_escape(detail)}</div></article>'
        )
    return '<div class="metrics">' + "".join(rendered) + "</div>"


def _is_numeric(text):
    return bool(_NUMERIC_CELL.match(text.strip()))


def _cell(value):
    """Render one table cell with presentation-only treatment; text content is never recomputed."""
    text = str(value)
    stripped = text.strip()
    if _is_numeric(stripped):
        return f'<td class="num">{_escape(text)}</td>'
    tone = _BADGE_TONE.get(stripped.replace("_", " ").casefold())
    if tone:
        return f'<td><span class="badge {tone}">{_escape(stripped.replace("_", " "))}</span></td>'
    arm = _ARM_ID.match(stripped)
    if arm:
        return (
            f'<td class="rid" title="{_escape(stripped)}"><details><summary>'
            f'<span class="rid-name">{_escape(arm.group("name"))}</span>'
            f'<span class="rid-meta">{_escape(arm.group("rg"))} · {_escape(arm.group("type"))}</span>'
            f"</summary><code>{_escape(stripped)}</code></details></td>"
        )
    if _SNAKE.match(stripped):
        return f'<td title="{_escape(stripped)}">{_escape(stripped.replace("_", " "))}</td>'
    if any(len(token) > 32 for token in text.split()):
        return f'<td class="long">{_escape(text)}</td>'
    return f"<td>{_escape(text)}</td>"


def _table(columns, rows, numeric_columns):
    head = "".join(
        ('<th scope="col" class="num">' if numeric_columns[i] else '<th scope="col">')
        + f"{_escape(column)}</th>"
        for i, column in enumerate(columns)
    )
    body = "".join("<tr>" + "".join(_cell(cell) for cell in row) + "</tr>" for row in rows)
    return (
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _sections(model):
    rendered = []
    for index, section in enumerate(_list(model.get("sections"), "sections")):
        if not isinstance(section, dict):
            raise ValueError(f"sections[{index}] must be an object")
        title = _required_text(section, "title")
        description = str(section.get("description") or "")
        columns = [str(item) for item in _list(section.get("columns"), "columns")]
        rows = _list(section.get("rows"), "rows")
        empty_message = str(section.get("emptyMessage") or "No rows.")
        if rows and not columns:
            raise ValueError(f"sections[{index}].columns is required when rows exist")
        for row_index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(columns):
                raise ValueError(
                    f"sections[{index}].rows[{row_index}] must match columns"
                )

        if rows:
            numeric_columns = [
                all(_is_numeric(str(row[i])) for row in rows if str(row[i]).strip() not in {"", "—"})
                and any(str(row[i]).strip() not in {"", "—"} for row in rows)
                for i in range(len(columns))
            ]
            if len(rows) > _COLLAPSE_AFTER:
                hidden = len(rows) - _VISIBLE_ROWS
                content = (
                    _table(columns, rows[:_VISIBLE_ROWS], numeric_columns)
                    + f'<details class="more-rows"><summary>Show {hidden} more rows</summary>'
                    + _table(columns, rows[_VISIBLE_ROWS:], numeric_columns)
                    + "</details>"
                )
            else:
                content = _table(columns, rows, numeric_columns)
            count = f'<span class="count">{len(rows)} {"row" if len(rows) == 1 else "rows"}</span>'
        else:
            content = f'<p class="empty">{_escape(empty_message)}</p>'
            count = ""
        description_html = (
            f'<p class="section-description">{_escape(description)}</p>' if description else ""
        )
        rendered.append(
            f'<section class="card"><div class="section-head"><h2>{_escape(title)}</h2>{count}</div>'
            f"{description_html}{content}</section>"
        )
    return "".join(rendered)


def _value_format(chart, label):
    explicit = chart.get("valueFormat")
    if explicit is not None:
        if explicit not in _VALUE_FORMATS:
            raise ValueError("chart.valueFormat must be currency, percent, or number")
        return explicit
    lowered = label.casefold()
    if "percent" in lowered or "%" in label:
        return "percent"
    if "usd" in lowered or "$" in label:
        return "currency"
    return "number"


def _chart(model):
    chart = model.get("chart")
    if chart is None:
        return "", ""
    if not isinstance(chart, dict):
        raise ValueError("chart must be an object")
    chart_type = str(chart.get("type") or "bar")
    if chart_type not in {"bar", "line", "doughnut"}:
        raise ValueError("chart.type must be bar, line, or doughnut")
    label = _required_text(chart, "label")
    labels = _list(chart.get("labels"), "chart.labels")
    values = _list(chart.get("values"), "chart.values")
    if len(labels) != len(values):
        raise ValueError("chart labels and values must have equal length")
    tones = _list(chart.get("tones"), "chart.tones")
    if tones and len(tones) != len(values):
        raise ValueError("chart tones and values must have equal length")
    if any(tone is not None and tone not in _TONES for tone in tones):
        raise ValueError("chart.tones entries must be null or a valid tone")
    reference = chart.get("referenceValue")
    if reference is not None and (
        isinstance(reference, bool) or not isinstance(reference, (int, float))
    ):
        raise ValueError("chart.referenceValue must be a number")
    orientation = chart.get("orientation")
    if orientation is not None and orientation not in {"vertical", "horizontal"}:
        raise ValueError("chart.orientation must be vertical or horizontal")
    heading = _escape(str(chart.get("title") or label))
    if not labels:
        return (
            f'<section class="card chart-card"><h2>{heading}</h2><p class="empty">No data points for this period.</p></section>',
            "",
        )
    str_labels = [str(item) for item in labels]
    if orientation is None:
        orientation = (
            "horizontal"
            if chart_type == "bar" and (len(str_labels) > 8 or max(len(x) for x in str_labels) > 14)
            else "vertical"
        )
    data = {
        "type": chart_type,
        "label": label,
        "labels": str_labels,
        "values": values,
        "tones": tones or None,
        "format": _value_format(chart, label),
        "horizontal": orientation == "horizontal",
        "reference": reference,
        "referenceLabel": str(chart.get("referenceLabel") or ""),
        "palette": {
            "brand": PALETTE["brand"],
            "good": PALETTE["good"],
            "warning": "#da3b01",
            "critical": PALETTE["critical"],
            "neutral": "#8a8886",
            "grid": "#ededed",
            "text": PALETTE["muted"],
        },
    }
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    markup = (
        f'<section class="card chart-card"><h2>{heading}</h2>'
        f'<div class="chart-wrap"><canvas id="finops-chart" role="img" aria-label="{heading}"></canvas></div></section>'
    )
    script = f"""
<script nonce="{{REPORT_NONCE}}">
const chartData = {payload};
const chartCanvas = document.getElementById('finops-chart');
if (chartCanvas && window.Chart) {{
  const p = chartData.palette;
  const fmt = (v) => {{
    const n = Number(v);
    if (chartData.format === 'currency') {{
      return '$' + n.toLocaleString('en-US', {{maximumFractionDigits: Math.abs(n) < 10 ? 2 : 0}});
    }}
    if (chartData.format === 'percent') {{ return n.toLocaleString('en-US', {{maximumFractionDigits: 1}}) + '%'; }}
    return n.toLocaleString('en-US', {{maximumFractionDigits: 2}});
  }};
  const toneColor = (t) => (t && p[t]) ? p[t] : p.brand;
  const series = ['#0f6cbd', '#5c2e91', '#038387', '#ca5010', '#4f6bed', '#8764b8', '#00b7c3', '#c239b3'];
  let colors;
  if (chartData.type === 'doughnut') {{
    colors = chartData.values.map((_, i) => series[i % series.length]);
  }} else if (chartData.tones) {{
    colors = chartData.tones.map(toneColor);
  }} else {{
    colors = chartData.values.map(() => p.brand);
  }}
  const isLine = chartData.type === 'line';
  const valueAxis = chartData.horizontal ? 'x' : 'y';
  const scales = {{}};
  if (chartData.type !== 'doughnut') {{
    const axisCommon = {{grid: {{color: p.grid}}, ticks: {{color: p.text}}, border: {{display: false}}}};
    scales.x = Object.assign({{}}, axisCommon);
    scales.y = Object.assign({{}}, axisCommon);
    scales[valueAxis] = Object.assign({{}}, axisCommon, {{beginAtZero: true, ticks: {{color: p.text, callback: (v) => fmt(v)}}}});
    const categoryAxis = chartData.horizontal ? 'y' : 'x';
    scales[categoryAxis].grid = {{display: false}};
    // Truncate long category labels on narrow screens; the tooltip keeps the full label.
    scales[categoryAxis].ticks = {{color: p.text, autoSkip: !chartData.horizontal, maxRotation: isLine ? 0 : 50, callback: function (value) {{
      const label = String(this.getLabelForValue(value));
      const limit = chartCanvas.parentElement.clientWidth < 560 ? 20 : 36;
      if (label.length <= limit) return label;
      const tail = Math.floor((limit - 1) / 2);
      return label.slice(0, limit - 1 - tail) + '\u2026' + label.slice(label.length - tail);
    }}}};
    if (chartData.reference !== null) {{
      const peak = Math.max.apply(null, chartData.values.map(Number).concat([chartData.reference]));
      scales[valueAxis].suggestedMax = peak * 1.08;
    }}
  }}
  const referenceLine = {{
    id: 'finopsReference',
    afterDatasetsDraw(chart) {{
      if (chartData.reference === null) return;
      const scale = chart.scales[valueAxis];
      const area = chart.chartArea;
      const at = scale.getPixelForValue(chartData.reference);
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = p.critical;
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      if (chartData.horizontal) {{ ctx.moveTo(at, area.top); ctx.lineTo(at, area.bottom); }}
      else {{ ctx.moveTo(area.left, at); ctx.lineTo(area.right, at); }}
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = p.critical;
      ctx.font = '600 12px "Segoe UI", sans-serif';
      const text = chartData.referenceLabel || fmt(chartData.reference);
      if (chartData.horizontal) {{
        const fitsRight = at + 6 + ctx.measureText(text).width <= area.right;
        ctx.textAlign = fitsRight ? 'left' : 'right';
        ctx.fillText(text, fitsRight ? at + 6 : at - 6, area.top - 6);
      }}
      else {{ ctx.textAlign = 'right'; ctx.fillText(text, area.right, at - 6); }}
      ctx.restore();
    }}
  }};
  if (chartData.horizontal) {{
    chartCanvas.parentElement.style.height = Math.max(240, chartData.labels.length * 34 + 60) + 'px';
  }}
  new Chart(chartCanvas, {{
    type: chartData.type,
    data: {{
      labels: chartData.labels,
      datasets: [{{
        label: chartData.label,
        data: chartData.values,
        borderColor: isLine ? p.brand : colors,
        backgroundColor: isLine ? 'rgba(15, 108, 189, 0.12)' : colors,
        pointBackgroundColor: p.brand,
        pointRadius: chartData.values.length > 45 ? 0 : 3,
        fill: isLine,
        borderWidth: isLine ? 2 : 0,
        borderRadius: isLine ? 0 : 4,
        maxBarThickness: 36,
        tension: 0.25
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      layout: {{padding: {{top: chartData.reference !== null && chartData.horizontal ? 18 : 0}}}},
      animation: false,
      indexAxis: chartData.horizontal ? 'y' : 'x',
      scales: chartData.type === 'doughnut' ? {{}} : scales,
      plugins: {{
        legend: {{display: chartData.type === 'doughnut', position: 'right'}},
        tooltip: {{callbacks: {{label: (c) => ' ' + fmt(chartData.type === 'doughnut' ? c.parsed : (chartData.horizontal ? c.parsed.x : c.parsed.y))}}}}
      }}
    }},
    plugins: [referenceLine]
  }});
}}
</script>"""
    return markup, script


def _display_time(value):
    """Format an ISO timestamp as 'YYYY-MM-DD HH:MM UTC'; other text is returned unchanged."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _scope_label(scopes):
    if not scopes:
        return "Scope not supplied"
    first = scopes[0]
    match = re.match(r"^/subscriptions/([^/\s]+)(?:/resourceGroups/([^/\s]+))?", first, re.IGNORECASE)
    if match:
        first = f"RG {match.group(2)}" if match.group(2) else f"Subscription {match.group(1)}"
    extra = f" +{len(scopes) - 1} more" if len(scopes) > 1 else ""
    return first + extra


def _status_chips(partial, metric_items, note_count):
    chips = []
    if partial:
        chips.append('<span class="chip warning">Partial data</span>')
    else:
        chips.append('<span class="chip good">Complete data</span>')
    critical = sum(1 for item in metric_items if item[3] == "critical")
    watch = sum(1 for item in metric_items if item[3] == "warning")
    if critical:
        chips.append(f'<span class="chip critical">{critical} need{"s" if critical == 1 else ""} action</span>')
    if watch:
        chips.append(f'<span class="chip warning">{watch} to watch</span>')
    if note_count:
        chips.append(f'<span class="chip neutral">{note_count} data {"note" if note_count == 1 else "notes"}</span>')
    return "".join(chips)


_STYLE = """
:root {{ color-scheme: light; font-family: "Segoe UI","Segoe UI Web (West European)",-apple-system,BlinkMacSystemFont,Roboto,"Helvetica Neue",sans-serif; color:{text}; background:{canvas}; font-size:15px; line-height:1.45; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px; }}
main {{ max-width:1240px; margin:0 auto; }}
h1,h2,p {{ margin-top:0; }}
.hero {{ background:linear-gradient(120deg,{brand_dark} 0%,{brand} 100%); color:#fff; border-radius:10px; padding:24px 28px; margin-bottom:16px; }}
.eyebrow {{ font-size:.78rem; font-weight:600; letter-spacing:.06em; text-transform:uppercase; opacity:.92; margin-bottom:6px; }}
.hero h1 {{ font-size:1.9rem; font-weight:600; margin-bottom:6px; }}
.hero .subtitle {{ color:#fff; max-width:860px; margin-bottom:14px; }}
.meta {{ display:flex; flex-wrap:wrap; gap:8px 18px; align-items:center; font-size:.86rem; }}
.meta-item {{ color:#fff; }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:12px; }}
.chip {{ display:inline-block; padding:3px 10px; border-radius:999px; font-size:.8rem; font-weight:600; background:#fff; }}
.chip.good {{ color:{good}; }} .chip.warning {{ color:{warning}; }} .chip.critical {{ color:{critical}; }} .chip.neutral {{ color:{muted}; }}
.banner {{ border-radius:8px; padding:12px 16px; margin-bottom:16px; border:1px solid; border-left-width:4px; }}
.banner.partial {{ background:{warning_bg}; border-color:#f4bfab; border-left-color:{warning}; color:{text}; }}
.banner strong {{ color:{warning}; }}
.card {{ background:#fff; border:1px solid {border}; border-radius:8px; box-shadow:0 1px 2px rgba(0,0,0,.06); padding:20px 22px; margin-bottom:16px; }}
.section-head {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; }}
h2 {{ font-size:1.15rem; font-weight:600; margin-bottom:6px; color:{text}; }}
.count {{ color:{muted}; font-size:.8rem; white-space:nowrap; }}
.section-description {{ color:{muted}; font-size:.9rem; margin-bottom:12px; }}
.metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; margin-bottom:16px; }}
.metric {{ background:#fff; border:1px solid {border}; border-left:4px solid #c8c8c8; border-radius:8px; padding:14px 16px; box-shadow:0 1px 2px rgba(0,0,0,.06); }}
.metric.good {{ border-left-color:{good}; background:{good_bg}; }}
.metric.warning {{ border-left-color:{warning}; background:{warning_bg}; }}
.metric.critical {{ border-left-color:{critical}; background:{critical_bg}; }}
.metric-label {{ color:{muted}; font-size:.84rem; font-weight:600; display:flex; justify-content:space-between; gap:8px; }}
.metric-value {{ font-size:1.7rem; font-weight:700; margin:4px 0 2px; color:{text}; font-variant-numeric:tabular-nums; }}
.metric-detail {{ color:{muted}; font-size:.82rem; }}
.tone-label {{ font-size:.7rem; text-transform:uppercase; letter-spacing:.05em; }}
.metric.good .tone-label {{ color:{good}; }} .metric.warning .tone-label {{ color:{warning}; }} .metric.critical .tone-label {{ color:{critical}; }}
.notes {{ background:{info_bg}; border:1px solid #b4d6fa; border-radius:8px; padding:12px 16px; margin-bottom:16px; }}
.notes summary {{ cursor:pointer; font-weight:600; color:{brand_dark}; }}
.notes .hint {{ font-weight:400; color:{muted}; font-size:.86rem; }}
.notes ul {{ margin:10px 0 0; padding-left:20px; font-size:.88rem; }} .notes li {{ margin-bottom:6px; }}
.table-wrap {{ overflow-x:auto; border:1px solid {border}; border-radius:6px; }}
table {{ width:100%; border-collapse:collapse; font-size:.88rem; }}
th,td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #ededed; vertical-align:top; }}
thead th {{ position:sticky; top:0; background:#fafafa; color:{muted}; font-size:.76rem; font-weight:600; text-transform:uppercase; letter-spacing:.03em; border-bottom:1px solid {border}; }}
tbody tr:nth-child(even) {{ background:#fcfcfc; }} tbody tr:hover {{ background:{info_bg}; }}
tbody tr:last-child td {{ border-bottom:none; }}
th.num,td.num {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
.badge {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:.76rem; font-weight:600; white-space:nowrap; border:1px solid; }}
.badge.good {{ color:{good}; background:{good_bg}; border-color:#9fd89f; }}
.badge.warning {{ color:{warning}; background:{warning_bg}; border-color:#f4bfab; }}
.badge.critical {{ color:{critical}; background:{critical_bg}; border-color:#eeacb2; }}
.badge.neutral {{ color:{muted}; background:{neutral_bg}; border-color:{border}; }}
td.rid details summary {{ cursor:pointer; list-style:none; }}
td.rid summary::-webkit-details-marker {{ display:none; }}
.rid-name {{ display:block; font-weight:600; color:{brand_dark}; }}
.rid-meta {{ display:block; color:{muted}; font-size:.78rem; }}
td.rid code {{ display:block; margin-top:4px; font-size:.75rem; color:{muted}; overflow-wrap:anywhere; }}
.more-rows {{ margin-top:10px; }} .more-rows > summary {{ cursor:pointer; color:{brand}; font-weight:600; font-size:.88rem; margin-bottom:8px; }}
.chart-wrap {{ height:320px; position:relative; }}
.empty {{ color:{muted}; font-style:italic; margin-bottom:0; }}
code,td.long {{ overflow-wrap:anywhere; }}
code {{ font-family:Consolas,"Cascadia Mono",monospace; }}
.provenance {{ color:{muted}; font-size:.84rem; }}
.provenance h2 {{ font-size:1rem; }}
.provenance ul {{ margin:0 0 10px; padding-left:20px; }}
.provenance p {{ margin-bottom:0; }}
@media (max-width:640px) {{
  body {{ padding:10px; }} .hero {{ padding:18px; }} .hero h1 {{ font-size:1.45rem; }} .card {{ padding:14px; }}
  .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }}
  .metric {{ padding:10px 12px; }} .metric-value {{ font-size:1.3rem; }}
  th,td {{ padding:7px 8px; }}
  table {{ min-width:680px; }} td.long,td.rid {{ min-width:180px; }}
  .chart-wrap {{ height:260px; }}
}}
@media print {{
  body {{ padding:0; background:#fff; }} :root {{ background:#fff; }}
  .hero,.metric,.badge,.chip {{ -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
  .card,.metric {{ box-shadow:none; break-inside:avoid; }}
  details::details-content {{ content-visibility:visible; display:block; }}
  .more-rows > summary,td.rid summary::after {{ display:none; }}
  thead th {{ position:static; }}
}}
""".format(**PALETTE)


def render_report(model):
    """Return deterministic self-contained report HTML for one structured model."""

    if not isinstance(model, dict):
        raise ValueError("model must be an object")
    title = _required_text(model, "title")
    description = str(model.get("description") or "")
    refreshed_at = _required_text(model, "refreshedAt")
    scopes = [str(item) for item in _list(model.get("scopeSummary"), "scopeSummary")]
    warnings = [str(item) for item in _list(model.get("warnings"), "warnings")]
    partial = bool(model.get("partial"))
    metric_items = _metric_items(model)

    warning_items = list(warnings)
    if partial and not warning_items:
        warning_items.append("Partial data: one or more source pulls were incomplete.")

    banner_html = ""
    if partial:
        banner_html = (
            '<aside class="banner partial" role="status"><strong>Partial data.</strong> '
            "One or more source pulls were incomplete, so totals may be understated. "
            "Review the data notes before acting on these figures.</aside>"
        )
    notes_html = ""
    if warning_items:
        notes_html = (
            '<aside class="notes"><details><summary>Data quality and methodology notes '
            f'({len(warning_items)}) <span class="hint">— read before acting on figures</span></summary><ul>'
            + "".join(f"<li>{_escape(item)}</li>" for item in warning_items)
            + "</ul></details></aside>"
        )

    scope_html = (
        "<ul>" + "".join(f"<li><code>{_escape(scope)}</code></li>" for scope in scopes) + "</ul>"
        if scopes
        else '<p class="empty">No managed scope summary was supplied.</p>'
    )
    chart_markup, chart_script = _chart(model)
    refreshed_display = _escape(_display_time(refreshed_at))
    chips = _status_chips(partial, metric_items, len(warning_items))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{_CSP}">
<title>{_escape(title)}</title>
<style nonce="{{REPORT_NONCE}}">{_STYLE}</style>
{_CHART_JS if chart_script else ""}
</head>
<body>
<main>
<header class="hero"><div class="eyebrow">Azure FinOps · Live Report</div><h1>{_escape(title)}</h1><p class="subtitle">{_escape(description)}</p><div class="meta"><span class="meta-item refreshed">Last refreshed: {refreshed_display}</span><span class="meta-item">{_escape(_scope_label(scopes))}</span></div><div class="chips">{chips}</div></header>
{banner_html}
{_metrics(metric_items)}
{chart_markup}
{notes_html}
{_sections(model)}
<footer class="card provenance"><h2>Managed scope and provenance</h2>{scope_html}<p>Static snapshot generated deterministically by the FinOps SRE Agent pack. Figures refresh only when the scheduled task runs again; last refreshed {refreshed_display}.</p></footer>
</main>
{chart_script}
</body>
</html>
"""


def write_report(model, output_path):
    """Render a report and write it to output_path, returning the path."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(model), encoding="utf-8")
    return str(path)
