import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_samples", ROOT / "docs" / "samples" / "generate_samples.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_sample_report_builds_and_renders():
    gen = _load_generator()
    assert len(gen.REPORTS) == 12
    for name, build in gen.REPORTS.items():
        html = gen.renderer.render_report(build())
        assert "<html" in html.lower(), name
        assert (ROOT / "docs" / "samples" / f"{name}.html").exists(), name
        assert (ROOT / "docs" / "images" / "reports" / f"{name}.png").exists(), name


def test_sample_data_uses_placeholder_subscription_only():
    for path in (ROOT / "docs" / "samples").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "00000000-0000-0000-0000-000000000000" in text, path.name
        assert not re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}\b", text.replace("chart.js@", "")), path.name
