"""Render an app tool's Jinja templates in a short-lived, resource-capped process.

A tool's templates come from an uploaded app's ``portal.json`` — untrusted
content, and the one place it runs on the server. The sandbox keeps a template
away from Python internals but not from exhausting resources: nested loops can
spin for hours, and a single call such as ``'x'.ljust(10**9)``,
``lipsum(10**6)`` or a constant ``7 ** 10**9`` allocates gigabytes or burns
minutes inside C code that no in-process check can interrupt. So
``portal.app_tools`` runs them here instead, in a fresh interpreter capped on
memory and CPU time, under a wall-clock timeout the parent enforces by killing
the process.

Run as a script, not imported: ``python -I template_worker.py``. It reads one
JSON request on stdin::

    {"templates": {"<name>": {"source": "...", "autoescape": true}, ...},
     "context": {...}}

and writes ``{"ok": {"<name>": "<output>", ...}}`` or ``{"error": "<message>"}``
to stdout. It imports nothing from the portal and gets a minimal environment,
so the child holds no portal state or secrets.
"""
import json
import sys

MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
CPU_LIMIT_SECONDS = 10
MAX_OUTPUT_CHARS = 4 * 1024 * 1024


def _limit_resources() -> None:
    try:
        import resource
    except ImportError:  # not POSIX — the parent's timeout still applies
        return
    for limit, value in (
        (resource.RLIMIT_AS, MEMORY_LIMIT_BYTES),
        (resource.RLIMIT_CPU, CPU_LIMIT_SECONDS),
    ):
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            pass  # e.g. macOS refuses RLIMIT_AS; the timeout and output cap remain


def main() -> None:
    _limit_resources()
    request = json.load(sys.stdin)
    try:
        from jinja2 import StrictUndefined
        from jinja2.sandbox import SandboxedEnvironment

        envs = {
            flag: SandboxedEnvironment(autoescape=flag, undefined=StrictUndefined)
            for flag in (True, False)
        }
        context = request["context"]
        out = {}
        total = 0
        for name, spec in request["templates"].items():
            env = envs[bool(spec["autoescape"])]
            text = env.from_string(spec["source"]).render(**context)
            total += len(text)
            if total > MAX_OUTPUT_CHARS:
                raise ValueError("template output is too large")
            out[name] = text
        result = {"ok": out}
    except MemoryError:
        result = {"error": "template used too much memory"}
    except Exception as e:  # undefined var, syntax error, sandbox violation
        result = {"error": str(e) or type(e).__name__}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
