#!/usr/bin/env python3
"""Block a reviewed list of privacy-sensitive sinks in ``backend/app``.

This is a developer regression barrier for the named modules and distribution
families below.  It is not a complete information-flow analysis, dependency
sandbox, or transitive package audit.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_APP = PROJECT_ROOT / "backend" / "app"
REQUIREMENTS = (
    PROJECT_ROOT / "backend" / "requirements.txt",
    PROJECT_ROOT / "backend" / "requirements-dev.txt",
    PROJECT_ROOT / "deploy/requirements.in",
    PROJECT_ROOT / "deploy/requirements.lock",
    PROJECT_ROOT / "deploy/build-requirements.lock",
)

FORBIDDEN_ROOT_MODULES = frozenset(
    {
        "aiohttp",
        "analytics",
        "boto3",
        "datadog",
        "dbm",
        "httpx",
        "newrelic",
        "opentelemetry",
        "posthog",
        "psycopg2",
        "pymongo",
        "redis",
        "requests",
        "sentry_sdk",
        "shelve",
        "sqlalchemy",
        "sqlite3",
    }
)
FORBIDDEN_EXACT_MODULES = frozenset(
    {
        "ftplib",
        "http.client",
        "logging.handlers",
        "smtplib",
        "socket",
        "urllib.request",
    }
)
FORBIDDEN_DISTRIBUTIONS = frozenset(
    module.replace("_", "-") for module in FORBIDDEN_ROOT_MODULES
)
FORBIDDEN_DISTRIBUTION_PREFIXES = (
    "datadog-",
    "opentelemetry-",
    "psycopg2-",
    "sentry-",
)


class PrivacyDependencyFailure(RuntimeError):
    pass


def _root_module(value: str) -> str:
    return value.split(".", maxsplit=1)[0].lower()


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _reject_module(module: str, path: Path, line: int) -> None:
    normalized = module.lower()
    if (
        _root_module(normalized) in FORBIDDEN_ROOT_MODULES
        or normalized in FORBIDDEN_EXACT_MODULES
    ):
        raise PrivacyDependencyFailure(
            f"{path}: line {line} imports privacy-sensitive dependency {module!r}; "
            "update the data flow, scrubber contract and canary tests first"
        )


def check_python_source(path: Path) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as error:
        raise PrivacyDependencyFailure(f"cannot safely parse {path}: {error}") from error

    import_module_names: set[str] = set()
    importlib_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _reject_module(alias.name, path, node.lineno)
                if alias.name == "importlib" or (
                    alias.name.startswith("importlib.") and alias.asname is None
                ):
                    importlib_aliases.add(alias.asname or "importlib")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                _reject_module(node.module, path, node.lineno)
                for alias in node.names:
                    _reject_module(
                        f"{node.module}.{alias.name}",
                        path,
                        node.lineno,
                    )
            if node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        import_module_names.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_dynamic_import = False
        if isinstance(node.func, ast.Name):
            is_dynamic_import = node.func.id in import_module_names | {"__import__"}
        elif (
            isinstance(node.func, ast.Call)
            and isinstance(node.func.func, ast.Name)
            and node.func.func.id == "getattr"
            and len(node.func.args) >= 2
            and isinstance(node.func.args[0], ast.Name)
            and node.func.args[0].id in importlib_aliases
            and _static_string(node.func.args[1]) == "import_module"
        ):
            is_dynamic_import = True
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in importlib_aliases
        ):
            is_dynamic_import = True
        if not is_dynamic_import:
            continue
        if not node.args:
            raise PrivacyDependencyFailure(
                f"{path}: line {node.lineno} has an unprovable dynamic import target"
            )
        module = _static_string(node.args[0])
        if module is None:
            raise PrivacyDependencyFailure(
                f"{path}: line {node.lineno} has an unprovable dynamic import target"
            )
        _reject_module(module, path, node.lineno)


def _requirement_name(line: str) -> str | None:
    line = line.split("#", maxsplit=1)[0].strip()
    if not line or line.startswith(("-r", "--requirement", "-c", "--constraint")):
        return None
    if line.startswith(("-e", "--editable", "git+", "http:", "https:", "file:")):
        raise PrivacyDependencyFailure(
            "URL/editable requirements require explicit privacy review"
        )
    match = re.match(r"(?P<name>[A-Za-z0-9_.-]+)", line)
    if not match:
        raise PrivacyDependencyFailure(f"cannot parse requirement line: {line}")
    return re.sub(r"[-_.]+", "-", match.group("name")).lower()


def check_requirements(path: Path) -> None:
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        try:
            name = _requirement_name(line)
        except PrivacyDependencyFailure as error:
            raise PrivacyDependencyFailure(f"{path}: line {line_number}: {error}") from error
        if name is not None and (
            name in FORBIDDEN_DISTRIBUTIONS
            or name.startswith(FORBIDDEN_DISTRIBUTION_PREFIXES)
        ):
            raise PrivacyDependencyFailure(
                f"{path}: line {line_number} adds privacy-sensitive dependency "
                f"{name!r}; complete privacy review first"
            )


def check_repository() -> None:
    for path in sorted(BACKEND_APP.rglob("*.py")):
        check_python_source(path)
    for path in REQUIREMENTS:
        check_requirements(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args()
    try:
        check_repository()
    except PrivacyDependencyFailure as error:
        print(f"PRIVACY DEPENDENCY CHECK FAILED: {error}", file=sys.stderr)
        return 1
    print("PRIVACY DEPENDENCY CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
