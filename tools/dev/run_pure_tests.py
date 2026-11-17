"""Minimal runner (standard library only) for the pure-function tests, when pytest is not available.

Runs the test_* functions that take no arguments or only `monkeypatch`; the rest (conftest
fixtures, parametrize) is reported as SKIP, to be run with real pytest:
    python tools/dev/run_pure_tests.py services/infra/tests/test_node_health_controller.py ...
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import os
import sys
import traceback
import types


class MonkeyPatch:
    def __init__(self) -> None:
        self._undo: list = []

    def setattr(self, target, name, value=None, raising=True):
        old = getattr(target, name)
        self._undo.append(lambda: setattr(target, name, old))
        setattr(target, name, value)

    def setenv(self, name, value):
        old = os.environ.get(name)
        self._undo.append(lambda: os.environ.__setitem__(name, old) if old is not None else os.environ.pop(name, None))
        os.environ[name] = str(value)

    def delenv(self, name, raising=True):
        old = os.environ.get(name)
        self._undo.append(lambda: os.environ.__setitem__(name, old) if old is not None else None)
        os.environ.pop(name, None)

    def undo(self):
        while self._undo:
            self._undo.pop()()


def _stub_pytest() -> types.ModuleType:
    """Just enough for `import pytest` + pytest.raises/approx in simple tests."""
    m = types.ModuleType("pytest")

    @contextlib.contextmanager
    def raises(exc, match=None):
        try:
            yield
        except exc:
            return
        raise AssertionError(f"expected {exc}")

    def approx(v, rel=1e-6, abs=1e-12):  # noqa: A002
        class _A:
            def __eq__(self, other):
                return abs_(other - v) <= max(rel * abs_(v), abs)
        abs_ = __builtins__["abs"] if isinstance(__builtins__, dict) else __builtins__.abs
        return _A()

    class _Mark:
        def __getattr__(self, _):
            def deco(*a, **k):
                if a and callable(a[0]) and len(a) == 1 and not k:
                    return a[0]
                return lambda f: setattr(f, "_needs_pytest", True) or f
            return deco

    m.raises, m.approx, m.mark = raises, approx, _Mark()
    m.fixture = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
    return m


def main(paths: list[str]) -> int:
    sys.modules.setdefault("pytest", _stub_pytest())
    ok = fail = skip = 0
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {name}: import failed — {type(e).__name__}: {e}")
            fail += 1
            continue
        for fname, fn in inspect.getmembers(mod, inspect.isfunction):
            if not fname.startswith("test_") or fn.__module__ != mod.__name__:
                continue
            params = set(inspect.signature(fn).parameters)
            if not params <= {"monkeypatch"} or getattr(fn, "_needs_pytest", False):
                skip += 1
                print(f"SKIP  {name}::{fname} (needs pytest: {', '.join(sorted(params)) or 'mark'})")
                continue
            mp = MonkeyPatch()
            setup = getattr(mod, "setup_function", None)  # xunit-style hooks, as pytest runs them
            teardown = getattr(mod, "teardown_function", None)
            try:
                if callable(setup):
                    setup()
                fn(**({"monkeypatch": mp} if params else {}))
                ok += 1
            except Exception:  # noqa: BLE001
                fail += 1
                print(f"FAIL  {name}::{fname}\n" + traceback.format_exc(limit=3))
            finally:
                mp.undo()
                if callable(teardown):
                    teardown()
    print(f"\n{ok} passed, {fail} failed, {skip} skipped")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
