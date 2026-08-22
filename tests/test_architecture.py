"""AST-level regression checks for the package dependency direction."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1] / "src" / "orchestrator"
STDLIB = sys.stdlib_module_names | {"__future__"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    imports: set[str] = set()
    relative_path = path.relative_to(ROOT).with_suffix("")
    module_parts = ["orchestrator", *relative_path.parts]
    if relative_path.name == "__init__":
        module_parts.pop()
        package_parts = module_parts
    else:
        package_parts = module_parts[:-1]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent_levels = node.level - 1
                base_parts = package_parts[: len(package_parts) - parent_levels]
                if node.module:
                    imports.add(".".join([*base_parts, *node.module.split(".")]))
                else:
                    imports.update(".".join([*base_parts, alias.name]) for alias in node.names)
            elif node.module:
                imports.add(node.module)
    return imports


def _modules(package: str) -> list[Path]:
    return sorted((ROOT / package).rglob("*.py"))


def _assert_only_internal_or_stdlib(package: str, allowed: tuple[str, ...]) -> None:
    for path in _modules(package):
        for module in _imports(path):
            top = module.split(".", 1)[0]
            assert top in STDLIB or module.startswith(allowed), f"{path}: forbidden import {module}"


def test_domain_depends_only_on_domain_and_standard_library():
    _assert_only_internal_or_stdlib("domain", ("orchestrator.domain",))


def test_application_depends_only_on_domain_application_and_standard_library():
    _assert_only_internal_or_stdlib(
        "application", ("orchestrator.application", "orchestrator.domain")
    )


def test_infrastructure_never_depends_on_the_composition_root():
    for path in _modules("infra"):
        assert not any(module.startswith("orchestrator.main") for module in _imports(path)), path


def test_only_main_package_imports_concrete_infrastructure_adapters():
    for package in ("domain", "application"):
        for path in _modules(package):
            assert not any(module.startswith("orchestrator.infra") for module in _imports(path)), path


@pytest.mark.parametrize(
    ("package", "source"),
    [
        ("application", "from .. import infra\n"),
        ("application", "from .. import main\n"),
        ("domain", "from .. import infra\n"),
        ("domain", "from .. import main\n"),
    ],
)
def test_relative_parent_imports_cannot_bypass_layer_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: str, source: str
):
    root = tmp_path / "src" / "orchestrator"
    module_path = root / package / "forbidden.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text(source)
    monkeypatch.setattr(sys.modules[__name__], "ROOT", root)

    with pytest.raises(AssertionError):
        _assert_only_internal_or_stdlib(
            package,
            (f"orchestrator.{package}", "orchestrator.domain"),
        )


def test_composition_functions_exist_only_in_the_composition_root():
    definitions: list[Path] = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("compose_") for node in tree.body):
            definitions.append(path.relative_to(ROOT))
    assert definitions == [Path("main/composition.py")]
