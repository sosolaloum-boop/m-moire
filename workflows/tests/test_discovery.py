import ast
from pathlib import Path

import mistralai.workflows as workflows

WORKFLOWS_DIR = Path(__file__).parent.parent / "src/worker/workflows/"


def _find_workflow_decorated_classes() -> set[str]:
    found: set[str] = set()

    def is_workflow_decorator(node: ast.expr, imports: dict[str, str]) -> bool:
        if isinstance(node, ast.Call):
            node = node.func
        attributes: list[str] = []
        while isinstance(node, ast.Attribute):
            attributes.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name) or node.id not in imports:
            return False
        qualified_name = ".".join([imports[node.id], *reversed(attributes)])
        return qualified_name == "mistralai.workflows.workflow.define"

    for py_file in WORKFLOWS_DIR.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        tree = ast.parse(py_file.read_text())
        imports: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    imports[name] = alias.name if alias.asname else name
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                is_workflow_decorator(d, imports) for d in node.decorator_list
            ):
                found.add(node.name)
    return found


def test_all_workflow_classes_are_discovered() -> None:
    decorated = _find_workflow_decorated_classes()
    discovered = workflows.discover_all_workflows_in_package("worker.workflows")
    missing = decorated - {cls.__name__ for cls in discovered}
    assert not missing, f"Not discovered: {missing}"
