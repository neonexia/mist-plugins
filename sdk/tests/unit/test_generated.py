"""The generated client types match api/openapi.yaml (docs/design/sdk.md section 5.2)."""
import ast
import dataclasses
import hashlib
import importlib
import re
import sys
from pathlib import Path
from typing import TypedDict

import pytest

ROOT = Path(__file__).resolve().parents[4]
PKG = ROOT / "sdk" / "python" / "mist" / "_generated"
SPEC = ROOT / "api" / "openapi.yaml"
sys.path.insert(0, str(ROOT / "sdk" / "python" / "tools"))

import gen  # noqa: E402


def test_generated_files_match_the_spec():
    files = gen.generate(SPEC.read_text(encoding="utf-8"))
    assert sorted(files) == ["__init__.py", "models.py", "operations.py"]
    for name, content in files.items():
        assert (PKG / name).read_text(encoding="utf-8") == content, f"{name} is stale: run the generator"


def test_spec_hash_is_recorded():
    from mist._generated import operations

    assert operations.SPEC_SHA256 == hashlib.sha256(SPEC.read_bytes()).hexdigest()


def test_every_route_is_an_operation():
    from mist._generated.operations import OPERATIONS, Operation

    assert len(OPERATIONS) == 31
    quota = OPERATIONS["set_quota"]
    assert dataclasses.is_dataclass(quota)
    assert quota == Operation("set_quota", "PUT", "/mist/v1/buckets/{name}/quota", ("name",), (), True, quota.statuses)
    assert quota.statuses and all(isinstance(s, int) for s in quota.statuses)
    assert 200 in quota.statuses
    assert OPERATIONS["health"].path_params == () and not OPERATIONS["health"].has_body
    assert OPERATIONS["whoami"].query_params == ("role",)


def test_models_are_plain_typeddicts():
    from mist._generated import models

    assert type(models.Bucket) is type(TypedDict("X", {}))
    assert issubclass(models.Bucket, dict)
    stdlib = set(sys.stdlib_module_names)
    for name in [m for m in sys.modules if m == "mist" or m.startswith("mist.")]:
        del sys.modules[name]
    before = set(sys.modules)
    importlib.import_module("mist._generated")
    importlib.import_module("mist._generated.models")
    importlib.import_module("mist._generated.operations")
    new = {m.split(".")[0] for m in set(sys.modules) - before}
    assert new <= stdlib | {"mist"}, new - stdlib


def test_optional_fields_are_not_required():
    from mist._generated import models

    # NewBucket-like split: pick any schema with both kinds of key.
    both = [
        t for t in vars(models).values()
        if isinstance(t, type) and issubclass(t, dict) and hasattr(t, "__required_keys__")
        and t.__required_keys__ and t.__optional_keys__
    ]
    assert both, "the spec has object schemas with optional fields"
    err = models.Error
    assert {"error", "message"} <= set(err.__required_keys__)
    assert {"rule", "request_id"} <= set(err.__optional_keys__)
    assert not set(err.__required_keys__) & set(err.__optional_keys__)


def test_generated_source_has_no_3_11_names():
    for path in PKG.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b(NotRequired|Required|Self)\b", text), path.name
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                pytest.fail(f"{path.name}:{node.lineno} uses X | Y")


def test_generator_raises_on_unknown_constructs():
    base = "openapi: 3.1.0\ncomponents:\n  schemas:\n    A:\n      type: object\n      additionalProperties: false\n      properties:\n        x: {%s}\n"
    with pytest.raises(gen.GenError, match="allOf"):
        gen.generate(base % "allOf: [{type: string}]")
    with pytest.raises(gen.GenError, match="uuid"):
        gen.generate(base % "type: uuid")
    with pytest.raises(gen.GenError, match="additionalProperties"):
        gen.generate(base.replace("additionalProperties: false", "x: 1") % "type: string")
