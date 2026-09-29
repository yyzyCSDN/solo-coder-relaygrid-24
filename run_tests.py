"""Minimal test runner for environments without pytest.

Provides a ``pytest.raises`` shim and runs all ``test_*`` callables in the
given modules. Exits non-zero if any test fails.
"""
from __future__ import annotations

import contextlib
import importlib
import inspect
import io
import sys
import traceback
from contextlib import contextmanager


class _Raises:
    def __init__(self, exc, match=None):
        self.exc = exc
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"expected {self.exc.__name__}, got nothing")
        if not issubclass(exc_type, self.exc):
            return False
        if self.match is not None and self.match not in str(exc):
            raise AssertionError(
                f"pattern {self.match!r} not in {str(exc)!r}"
            )
        self.value = exc
        return True


class _CaptureResult:
    def __init__(self, out, err):
        self.out = out
        self.err = err

    def readouterr(self):
        return _CaptureResult(self.out.getvalue(), self.err.getvalue())


@contextmanager
def _capsys():
    old_out, old_err = sys.stdout, sys.stderr
    new_out, new_err = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = new_out, new_err
    try:
        yield _CaptureResult(new_out, new_err)
    finally:
        sys.stdout, sys.stderr = old_out, old_err


class _Mark:
    def __getattr__(self, name):
        def deco(fn):
            return fn
        return deco


class _Pytest:
    raises = staticmethod(lambda exc, match=None: _Raises(exc, match))
    mark = _Mark()


def _invoke(fn):
    """Invoke a test function, injecting supported fixtures."""
    params = inspect.signature(fn).parameters
    if "capsys" in params:
        with _capsys() as cap:
            fn(cap)
    else:
        fn()


def run(module_names):
    failures = 0
    passed = 0
    for name in module_names:
        mod = importlib.import_module(name)
        for attr in sorted(dir(mod)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(mod, attr)
            if not callable(fn):
                continue
            try:
                _invoke(fn)
                passed += 1
                print(f"PASS {name}.{attr}")
            except Exception:
                failures += 1
                print(f"FAIL {name}.{attr}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.modules["pytest"] = _Pytest()
    sys.path.insert(0, "src")
    sys.exit(run(sys.argv[1:]))
