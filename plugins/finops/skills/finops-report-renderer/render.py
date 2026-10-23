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


def _metrics(model):
    rendered = []
    for index, metric in enumerate(_list(model.get("metrics"), "metrics")):
        if not isinstance(metric, dict):
            raise ValueError(f"metrics[{index}] must be an object")
        label = _required_text(metric, "label")
        value = _required_text(metric, "value")
        detail = str(metric.get("detail") or "")
        tone = str(metric.get("tone") or "neutral").lower()
        if tone not in _TONES:
            raise ValueError(f"metrics[{index}].tone is invalid")
        rendered.append(
            f'<article class="metric {tone}"><div class="metric-label">{_escape(label)}</div>'
            f'<div class="metric-value">{_escape(value)}</div>'
            f'<div class="metric-detail">{_escape(detail)}</div></article>'
        )
    if not rendered:
        return '<p class="empty">No headline metrics were produced.</p>'
    return '<div class="metrics">' + "".join(rendered) + "</div>"


_NUMERIC_CELL = re.compile(r"^[-+]?[$€£]?[-+]?[\d.,]+\s?(%|[A-Z]{3}|AAU)?$")


def _cell(value):
    """Render one table cell: keep numbers on one line, let long tokens (resource IDs) wrap."""
    text = str(value)
    if _NUMERIC_CELL.match(text.strip()):
        return f'<td class="num">{_escape(text)}</td>'
    if any(len(token) > 32 for token in text.split()):
        return f'<td class="long">{_escape(text)}</td>'
    return f"<td>{_escape(text)}</td>"


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

        body = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(columns):
                raise ValueError(
                    f"sections[{index}].rows[{row_index}] must match columns"
                )
            body.append("<tr>" + "".join(_cell(cell) for cell in row) + "</tr>")

        if body:
            content = (
                "<div class=\"table-wrap\"><table><thead><tr>"
                + "".join(f"<th>{_escape(column)}</th>" for column in columns)
                + "</tr></thead><tbody>"
                + "".join(body)
                + "</tbody></table></div>"
            )
        else:
            content = f'<p class="empty">{_escape(empty_message)}</p>'
        rendered.append(
            f'<section><h2>{_escape(title)}</h2><p class="section-description">'
            f"{_escape(description)}</p>{content}</section>"
        )
    return "".join(rendered)


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
    heading = _escape(str(chart.get("title") or label))
    if not labels:
        return (
            f'<section><h2>{heading}</h2><p class="empty">No data points for this period.</p></section>',
            "",
        )
    data = {
        "type": chart_type,
        "label": label,
        "labels": [str(item) for item in labels],
        "values": values,
    }
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    markup = f'<section><h2>{heading}</h2><div class="chart-wrap"><canvas id="finops-chart"></canvas></div></section>'
    script = f"""
<script nonce="{{REPORT_NONCE}}">
const chartData = {payload};
const chartCanvas = document.getElementById('finops-chart');
if (chartCanvas && window.Chart) {{
  new Chart(chartCanvas, {{
    type: chartData.type,
    data: {{
      labels: chartData.labels,
      datasets: [{{
        label: chartData.label,
        data: chartData.values,
        borderColor: '#2563eb',
        backgroundColor: 'rgba(37, 99, 235, 0.18)',
        borderWidth: 2,
        tension: 0.25
      }}]
    }},
    options: {{responsive: true, maintainAspectRatio: false}}
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

    warning_items = list(warnings)
    if partial and not warning_items:
        warning_items.append("Partial data: one or more source pulls were incomplete.")
    warning_html = ""
    if warning_items:
        warning_html = '<aside class="warnings"><strong>Data quality</strong><ul>' + "".join(
            f"<li>{_escape(item)}</li>" for item in warning_items
        ) + "</ul></aside>"

    scope_html = (
        "<ul>" + "".join(f"<li><code>{_escape(scope)}</code></li>" for scope in scopes) + "</ul>"
        if scopes
        else '<p class="empty">No managed scope summary was supplied.</p>'
    )
    chart_markup, chart_script = _chart(model)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{_CSP}">
<title>{_escape(title)}</title>
<style nonce="{{REPORT_NONCE}}">
:root {{ color-scheme: light; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:#172033; background:#f5f7fb; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px; }}
main {{ max-width:1200px; margin:0 auto; }}
header,section,.metric,.warnings {{ background:#fff; border:1px solid #dce3ef; border-radius:12px; box-shadow:0 2px 8px rgba(15,23,42,.05); }}
header,section {{ padding:20px; margin-bottom:16px; }}
h1,h2,p {{ margin-top:0; }}
.subtitle,.section-description,.refreshed {{ color:#596780; }}
.metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:16px; }}
.metric {{ padding:16px; border-top:4px solid #94a3b8; }}
.metric.good {{ border-top-color:#16a34a; }} .metric.warning {{ border-top-color:#d97706; }} .metric.critical {{ border-top-color:#dc2626; }}
.metric-label,.metric-detail {{ color:#596780; font-size:.88rem; }} .metric-value {{ font-size:1.65rem; font-weight:700; margin:6px 0; }}
.warnings {{ padding:16px; margin-bottom:16px; border-color:#f59e0b; background:#fffbeb; }}
.warnings ul {{ margin-bottom:0; }}
.table-wrap {{ overflow-x:auto; }} table {{ width:100%; border-collapse:collapse; }}
th,td {{ padding:10px; text-align:left; border-bottom:1px solid #e5eaf2; }} th {{ color:#44516a; font-size:.84rem; text-transform:uppercase; }}
.chart-wrap {{ height:320px; }} .empty {{ color:#718096; font-style:italic; }}
code,td.long {{ overflow-wrap:anywhere; }} td.num {{ white-space:nowrap; }}
@media (max-width:640px) {{
  body {{ padding:12px; }} header,section {{ padding:14px; }} h1 {{ font-size:1.5rem; }}
  .metrics {{ grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:8px; }}
  .metric-value {{ font-size:1.35rem; }} th,td {{ padding:8px 6px; font-size:.86rem; }}
  table {{ min-width:720px; }} td.long {{ min-width:180px; }}
  .chart-wrap {{ height:240px; }}
}}
</style>
{_CHART_JS if chart_script else ""}
</head>
<body>
<main>
<header><h1>{_escape(title)}</h1><p class="subtitle">{_escape(description)}</p><p class="refreshed">Last refreshed: {_escape(_display_time(refreshed_at))}</p></header>
{warning_html}
{_metrics(model)}
{chart_markup}
<section><h2>Managed scope</h2>{scope_html}</section>
{_sections(model)}
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
