from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GUARD = PROJECT_ROOT / "scripts" / "verify_privacy_dependencies.py"


def _load_guard():
    assert GUARD.is_file(), "privacy dependency guard must be repository-owned"
    spec = importlib.util.spec_from_file_location(
        "verify_privacy_dependencies",
        GUARD,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "source",
    [
        'import sentry_sdk\n',
        'from sentry_sdk import init\n',
        'import importlib\nimportlib.import_module("sentry_sdk")\n',
        'import importlib.metadata\nimportlib.import_module("sentry_sdk")\n',
        'import importlib as loader\nloader.import_module("sen" + "try_sdk")\n',
        'import importlib\ngetattr(importlib, "import_module")("sentry_sdk")\n',
        'from importlib import import_module as load\nload("http" + "x")\n',
        '__import__("httpx")\n',
    ],
)
def test_privacy_dependency_guard_rejects_static_and_dynamic_import_forms(
    tmp_path,
    source,
):
    guard = _load_guard()
    source_path = tmp_path / "candidate.py"
    source_path.write_text(source, encoding="utf-8")

    with pytest.raises(guard.PrivacyDependencyFailure):
        guard.check_python_source(source_path)


def test_privacy_dependency_guard_preserves_allowed_standard_library_imports(
    tmp_path,
):
    guard = _load_guard()
    source_path = tmp_path / "candidate.py"
    source_path.write_text(
        "import json\nfrom pathlib import Path\n",
        encoding="utf-8",
    )

    guard.check_python_source(source_path)


@pytest.mark.parametrize(
    "source",
    [
        "from urllib.request import urlopen\n",
        "from urllib import request\n",
        "import http.client\n",
        "from http import client\n",
        "import smtplib\n",
        "import ftplib\n",
        "import socket\n",
        "import logging.handlers\n",
        "from logging import handlers\n",
        "import boto3\n",
        "import dbm\n",
        "import posthog\n",
        "import psycopg2\n",
        "import pymongo\n",
    ],
)
def test_privacy_dependency_guard_rejects_unreviewed_stdlib_outbound_sinks(
    tmp_path,
    source,
):
    guard = _load_guard()
    source_path = tmp_path / "candidate.py"
    source_path.write_text(source, encoding="utf-8")

    with pytest.raises(guard.PrivacyDependencyFailure):
        guard.check_python_source(source_path)


def test_privacy_dependency_guard_preserves_non_sink_siblings(tmp_path):
    guard = _load_guard()
    source_path = tmp_path / "candidate.py"
    source_path.write_text(
        "from urllib.parse import urlsplit\n"
        "from http import HTTPStatus\n"
        "import logging\n",
        encoding="utf-8",
    )

    guard.check_python_source(source_path)


def test_privacy_dependency_guard_checks_requirement_names(tmp_path):
    guard = _load_guard()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "fastapi==0.139.2\nsentry-sdk==2.0.0\n",
        encoding="utf-8",
    )

    with pytest.raises(guard.PrivacyDependencyFailure):
        guard.check_requirements(requirements)


@pytest.mark.parametrize(
    "requirement",
    [
        "opentelemetry-api==1.40.0\n",
        "opentelemetry-sdk==1.40.0\n",
        "opentelemetry-instrumentation-fastapi==0.61b0\n",
        "datadog-api-client==2.0.0\n",
        "boto3==1.42.76\n",
        "posthog==7.11.0\n",
        "psycopg2-binary==2.9.11\n",
        "pymongo==4.16.0\n",
        "file:///private/tmp/unreviewed.whl\n",
    ],
)
def test_privacy_dependency_guard_rejects_distribution_families_and_file_urls(
    tmp_path,
    requirement,
):
    guard = _load_guard()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(requirement, encoding="utf-8")

    with pytest.raises(guard.PrivacyDependencyFailure):
        guard.check_requirements(requirements)
