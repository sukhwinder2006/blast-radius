
"""Coverage-guided test selector for SQLGlot."""

import json
import re
import shlex
import sys
from pathlib import Path, PurePosixPath

DATA = Path("/data")
COVERAGE_FILE = Path("/app/coverage_map.json")
FUNCTION_COVERAGE_FILE = Path("/app/function_coverage.json")

MAX_WORKERS = 8
WORKER_COST = 2.0

# Tests not represented in line coverage.
SPECIAL_TESTS = {214, 1565, 1566, 1567, 1619}

BUILD_FILES = {
    "setup.py",
    "pyproject.toml",
    "setup.cfg",
    "MANIFEST.in",
}


# Optimizer SQL fixtures and the tests that load them.
OPTIMIZER_FIXTURE_TESTS = {
    "pushdown_predicates.sql": {1377},
    "qualify_columns__invalid.sql": {1383},
    "unnest_subqueries.sql": {1454},
    "annotate_types.sql": {1302},
    "simplify.sql": {1434},
    "pushdown_cte_alias_columns.sql": {1375},
    "merge_subqueries.sql": {1343},
    "pushdown_projections.sql": {1378},
    "qualify_columns_ddl.sql": {1382},
    "isolate_table_selects.sql": {1335},
}

def changed_paths(patch_path):
    """Extract old and new filenames from Git diff headers."""
    paths = set()

    try:
        for line in patch_path.read_text(errors="replace").splitlines():
            if not line.startswith("diff --git "):
                continue

            parts = shlex.split(line)

            if len(parts) != 4:
                return None

            for value in parts[2:4]:
                if value == "/dev/null":
                    continue

                if value.startswith(("a/", "b/")):
                    value = value[2:]

                value = str(PurePosixPath(value))

                if value and value != ".":
                    paths.add(value)

    except Exception:
        return None

    return paths if paths else None



def is_comment_only(patch_path):
    """Return True when all changed lines are Python comments."""
    current_path = ""
    found_change = False

    for line in patch_path.read_text(errors="replace").splitlines():
        if line.startswith("diff --git "):
            parts = shlex.split(line)
            current_path = parts[3][2:] if len(parts) == 4 else ""
            continue

        if not line.startswith(("+", "-")):
            continue

        if line.startswith(("+++", "---")):
            continue

        found_change = True
        content = line[1:].strip()

        if not content:
            continue

        if current_path.endswith((".py", ".pyi")) and content.startswith("#"):
            continue

        return False

    return found_change


def changed_hunks(patch_path):
    """Return original source line ranges changed by a patch."""
    result = {}
    current_path = None

    try:
        for line in patch_path.read_text(errors="replace").splitlines():
            if line.startswith("diff --git "):
                current_path = None

            elif line.startswith("--- a/"):
                current_path = line[6:]

            elif line.startswith("@@ ") and current_path:
                match = re.match(
                    r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
                    line
                )
                if match:
                    result.setdefault(current_path, []).append(
                        (int(match.group(1)), int(match.group(2) or 1))
                    )

    except Exception:
        return {}

    return result


def schedule(selected, test_by_number):
    """Find the cheapest LPT distribution across 1 to 8 workers."""
    if not selected:
        return []

    tests = sorted(
        (test_by_number[n] for n in selected),
        key=lambda t: (-t["seconds"], t["number"]),
    )

    best_workers = None
    best_cost = float("inf")

    for count in range(1, min(MAX_WORKERS, len(tests)) + 1):
        workers = [[] for _ in range(count)]
        loads = [0.0] * count

        for test in tests:
            lightest = min(range(count), key=lambda i: loads[i])
            workers[lightest].append(test["number"])
            loads[lightest] += test["seconds"]

        cost = max(loads) + WORKER_COST * count

        if cost < best_cost:
            best_cost = cost
            best_workers = workers

    return best_workers


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    coverage = json.loads(COVERAGE_FILE.read_text())
    function_tests = json.loads(
        FUNCTION_COVERAGE_FILE.read_text()
    )["function_tests"]

    test_by_number = {t["number"]: t for t in tests}
    test_by_id = {t["id"]: t["number"] for t in tests}

    file_tests = coverage["file_tests"]
    optimizer_tests = {
        number
        for number, test in test_by_number.items()
        if test["id"].startswith("tests/test_optimizer.py::")
    }

    executor_tests = {
        number
        for number, test in test_by_number.items()
        if test["id"].startswith("tests/test_executor.py::")
    }
    # Confirm that all special tests exist in the supplied test list.
    if not SPECIAL_TESTS.issubset(test_by_number):
        raise RuntimeError("A special test number is missing")

    patches = sorted((DATA / "commits").glob("*.patch"))

    for patch in patches:
        paths = changed_paths(patch)
        hunks_by_path = changed_hunks(patch)
        selected = set()
        fallback = False

        if paths is None:
            selected = set(test_by_number)
            fallback = True

        else:
            for path in paths:
                # Optimizer fixture changes: select their known consumer tests.
                if path.startswith("tests/fixtures/optimizer/") and path.endswith(".sql"):
                    fixture = Path(path).name
                    matches = OPTIMIZER_FIXTURE_TESTS.get(fixture)

                    if matches:
                        selected.update(matches)
                    else:
                        selected = set(test_by_number)
                        fallback = True
                        break

                # Test-file changes: select every numbered test in that file.
                elif path.startswith("tests/"):
                    matches = {
                        number
                        for test_id, number in test_by_id.items()
                        if test_id.startswith(path + "::")
                    }

                    if matches:
                        selected.update(matches)
                    else:
                        # A helper or new test file may affect other tests.
                        selected = set(test_by_number)
                        fallback = True
                        break

                # Build and native-extension changes.
                elif path.startswith("sqlglotc/") or path in BUILD_FILES:
                    selected.update({1565, 1566, 1567})

                # Prefer function-level coverage for changed source code.
                # Fall back to whole-file coverage whenever mapping is uncertain.
                elif path in file_tests:
                    ranges = hunks_by_path.get(path, [])
                    entries = function_tests.get(path, [])
                    function_selected = set()
                    mapping_safe = bool(ranges and entries) and path != "sqlglot/serde.py"

                    if mapping_safe:
                        for old_start, old_count in ranges:
                            if old_count == 0:
                                changed_lines = {
                                    max(1, old_start - 1),
                                    max(1, old_start),
                                }
                            else:
                                changed_lines = range(
                                    old_start,
                                    old_start + old_count,
                                )

                            matches = [
                                entry for entry in entries
                                if any(
                                    entry[0] <= line <= entry[1]
                                    for line in changed_lines
                                )
                            ]

                            if not matches:
                                mapping_safe = False
                                break

                            for entry in matches:
                                function_selected.update(entry[2])

                    if mapping_safe and function_selected:
                        selected.update(function_selected)
                    else:
                        selected.update(file_tests[path])

                # An unknown Python/source file is unsafe to ignore.
                elif (
                    path.startswith("sqlglot/")
                    or path.endswith((".py", ".pyi", ".c", ".rs"))
                ):
                    selected = set(test_by_number)
                    fallback = True
                    break

        # Expand selection for cross-component dependencies.
        if paths is not None:
            if any(
                path.startswith((
                    "sqlglot/optimizer/",
                    "sqlglot/parsers/",
                    "sqlglot/dialects/",
                ))
                for path in paths
            ):
                selected.update(optimizer_tests)

            if any(
                path.startswith("sqlglot/optimizer/")
                for path in paths
            ):
                selected.update(executor_tests)

        # Include tests that coverage could not observe.
        selected.update(SPECIAL_TESTS)
        workers = schedule(selected, test_by_number)

        print(
            json.dumps({
                "commit": patch.stem,
                "workers": workers,
            })
        )

        print(
            f"{patch.stem}: {len(selected)} tests"
            + (" (fallback: all tests)" if fallback else ""),
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
