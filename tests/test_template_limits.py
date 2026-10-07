"""Regression test: an app's tool templates can't hang or exhaust the server.

Run from anywhere:

    python3 tests/test_template_limits.py

**What's being pinned.** Tool templates come from an uploaded app's
``portal.json`` and are the one piece of uploaded content that runs on the
server. They rendered in-process in a Jinja sandbox, which blocks escapes but
not resource exhaustion: nested loops spun indefinitely, string repetition and
``|format`` widths allocated tens of MB per expression, and Jinja even
evaluated a constant ``7 ** <huge>`` while *compiling*, stalling the event loop.
They now render in ``portal/template_worker.py``: a separate process with a
memory cap (Linux), a CPU cap, an output cap, and a wall-clock timeout after
which the parent kills it. The second half checks real templates still render.
"""
import json
import subprocess
import sys
import time

import _harness as h

from portal import app_tools
from portal.apps import PortalAppTool

app_tools._RENDER_TIMEOUT_SECONDS = 3  # keep the runaway cases quick


def render(html: str, **args):
    tool = PortalAppTool(
        name="t", description="d",
        params=[{"name": "who", "type": "string"},
                {"name": "items", "type": "array", "fields": [
                    {"name": "desc", "type": "string"}, {"name": "qty", "type": "number"},
                    {"name": "rate", "type": "number"}]}],
        render={"html": html}, deliver={"kind": "download"},
    ).model_dump()
    ctx = app_tools._build_context(tool, args)
    start = time.monotonic()
    try:
        out = app_tools._render_templates({"html": (html, True)}, ctx)["html"]
    except app_tools.AppToolError as e:
        out = f"refused: {e}"
    return out, time.monotonic() - start


print("--- runaway templates are stopped ---")
for label, tpl in (
    ("nested loops (10^10 iterations)",
     "{% for a in range(100000) %}{% for b in range(100000) %}{% endfor %}{% endfor %}"),
    ("constant power folded at compile time",
     "{% if false %}{{ 7 ** 300000000 }}{% endif %}"),
    ("100 MB string from ljust", "{{ 'x'.ljust(100000000) }}"),
    ("100 MB from a format width", "{{ '%0100000000d'|format(1) }}"),
    ("huge lipsum", "{{ lipsum(1000000) }}"),
):
    out, took = render(tpl)
    h.check(f"{label}: refused", out.startswith("refused"), True)
    h.check("  ...within the timeout", took < app_tools._RENDER_TIMEOUT_SECONDS + 2, True)

if sys.platform.startswith("linux"):
    proc = subprocess.run(
        [sys.executable, "-I", str(app_tools._TEMPLATE_WORKER)],
        input=json.dumps({"templates": {"x": {"source": "{{ 'x'.ljust(2000000000)|length }}",
                                              "autoescape": False}}, "context": {}}).encode(),
        capture_output=True, timeout=30)
    h.check("2 GB allocation hits the worker's memory cap",
            "memory" in proc.stdout.decode(), True)

print("--- the sandbox still holds ---")
out, _ = render("{{ ''.__class__.__mro__ }}")
h.check("attribute escape refused", out.startswith("refused"), True)

print("--- must not regress: real templates ---")
invoice = (
    "<h1>Invoice for {{ who }}</h1>"
    "{% set ns = namespace(total=0) %}"
    "{% for i in items %}<tr><td>{{ i.desc }}</td><td>{{ '%.2f'|format(i.qty * i.rate) }}</td></tr>"
    "{% set ns.total = ns.total + i.qty * i.rate %}{% endfor %}"
    "<p>Total {{ '%.2f'|format(ns.total) }}</p>"
)
items = [{"desc": f"Line {n}", "qty": n, "rate": 2.5} for n in range(1, 201)]
out, took = render(invoice, who="<Acme & Co>", items=items)
h.check("200-line invoice renders", "Total 50250.00" in out, True)
h.check("  ...params still autoescaped", "&lt;Acme &amp; Co&gt;" in out, True)
h.check("  ...in well under a second", took < 1.0, True)
out, _ = render("{{ nope }}")
h.check("undefined variable reported", "nope" in out and out.startswith("refused"), True)

h.finish()
