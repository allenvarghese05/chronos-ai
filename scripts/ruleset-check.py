#!/usr/bin/env python3
"""ruleset-check: assert that required_status_checks contexts in
scripts/github-ruleset.json equal the top-level job keys in
.github/workflows/ci.yml.  Uses only the stdlib (json, re, sys, pathlib).

Exit 0 when the two sets are equal, 1 on mismatch or parse failure.
Usage: python3 scripts/ruleset-check.py [repo-root]
"""
import json
import re
import sys
from pathlib import Path


def load_ruleset_contexts(root: Path) -> set[str]:
    """Return the set of required_status_checks context strings from the ruleset JSON."""
    path = root / "scripts" / "github-ruleset.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot parse {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    for rule in data.get("rules", []):
        if rule.get("type") == "required_status_checks":
            checks = rule.get("parameters", {}).get("required_status_checks", [])
            return {c["context"] for c in checks}

    print(
        f"ERROR: no required_status_checks rule found in {path}",
        file=sys.stderr,
    )
    sys.exit(1)


def load_ci_jobs(root: Path) -> set[str]:
    """Return the set of top-level job names from .github/workflows/ci.yml.

    Parses the YAML structurally by finding the 'jobs:' key and then
    collecting all non-indented keys below it using a simple regex.
    This avoids any third-party YAML dependency.
    """
    path = root / ".github" / "workflows" / "ci.yml"
    try:
        text = path.read_text()
    except OSError as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    lines = text.splitlines()
    in_jobs = False
    jobs: set[str] = set()
    # A top-level YAML key: starts at column 0, is a valid identifier, followed by ':'
    top_key_re = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*):\s*(?:#.*)?$")
    # A jobs-block child key: exactly 2 spaces of indent, identifier, ':'
    job_key_re = re.compile(r"^  ([a-zA-Z][a-zA-Z0-9_-]*):\s*(?:#.*)?$")

    for line in lines:
        if line.rstrip() == "jobs:":
            in_jobs = True
            continue
        if in_jobs:
            m = top_key_re.match(line)
            if m:
                # We hit another top-level key; jobs block is over
                in_jobs = False
                continue
            m = job_key_re.match(line)
            if m:
                jobs.add(m.group(1))

    if not jobs:
        print(f"ERROR: no jobs found in {path}", file=sys.stderr)
        sys.exit(1)

    return jobs


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent

    contexts = load_ruleset_contexts(root)
    jobs = load_ci_jobs(root)

    only_ruleset = contexts - jobs
    only_ci = jobs - contexts

    if not only_ruleset and not only_ci:
        print(
            f"ruleset-check: OK — required_status_checks {sorted(contexts)} "
            f"match CI job names {sorted(jobs)}"
        )
        return 0

    print("ruleset-check: MISMATCH between scripts/github-ruleset.json and .github/workflows/ci.yml")
    if only_ruleset:
        print(
            f"  in ruleset but not in ci.yml: {sorted(only_ruleset)}\n"
            "  → add these job names to ci.yml or remove them from the ruleset"
        )
    if only_ci:
        print(
            f"  in ci.yml but not in ruleset: {sorted(only_ci)}\n"
            "  → add these job names to scripts/github-ruleset.json required_status_checks "
            "or remove the jobs from ci.yml"
        )
    print(
        "  NOTE: job names are frozen — the ruleset enforces them on every PR once enabled. "
        "See docs/plans/T0.2-plan.md and T6.7."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
