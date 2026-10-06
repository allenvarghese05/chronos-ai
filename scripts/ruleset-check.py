#!/usr/bin/env python3
"""ruleset-check: assert that required_status_checks contexts in
scripts/github-ruleset.json equal the GitHub status-check context names for
every job in .github/workflows/ci.yml.

GitHub uses a job's ``name:`` field as the status-check context when present
and falls back to the job key otherwise.  This script parses both, compares
the resulting sets, and also enforces our project convention that ``name:``
(when present) must equal the job key so the two values never silently drift.

Uses only the Python 3.12 stdlib (json, re, sys, pathlib, textwrap).

Exit 0 when everything matches, 1 on any mismatch or parse failure.

Usage:
    python3 scripts/ruleset-check.py [repo-root]
    python3 scripts/ruleset-check.py --self-test
"""
import json
import re
import sys
import textwrap
from pathlib import Path


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

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


def parse_ci_jobs(text: str) -> dict[str, str]:
    """Parse a ci.yml text and return {job_key: context_name}.

    GitHub uses the job ``name:`` field as the status-check context when it is
    present, and falls back to the job key otherwise.  This function:

    - Locates the ``jobs:`` top-level block (column 0).
    - Collects job keys at exactly 2-space indent.
    - For each job key, scans the following lines at exactly 4-space indent for
      a ``name:`` entry (stops at the next 2-space or 0-space key).
    - Returns the ``name:`` value when found, else the key itself.

    Raises ``ValueError`` with a descriptive message on parse failure.
    """
    lines = text.splitlines()

    # Regexes — anchored, exact indent, robust to trailing comments/spaces.
    top_key_re = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*):\s*(?:#.*)?$")
    job_key_re = re.compile(r"^  ([a-zA-Z][a-zA-Z0-9_-]*):\s*(?:#.*)?$")
    # name: value  (4-space indent inside a job block)
    name_val_re = re.compile(r"^    name:\s+([^\s#][^#]*)(?:\s*#.*)?$")

    in_jobs = False
    jobs: dict[str, str] = {}          # key -> context name
    current_key: str | None = None
    current_name: str | None = None

    def _flush() -> None:
        if current_key is not None:
            jobs[current_key] = current_name if current_name is not None else current_key

    for line in lines:
        stripped = line.rstrip()

        if not in_jobs:
            if stripped == "jobs:":
                in_jobs = True
            continue

        # Back to a top-level key → jobs block ended.
        if top_key_re.match(stripped):
            _flush()
            current_key = None
            current_name = None
            in_jobs = False
            continue

        # New job key at 2-space indent.
        m = job_key_re.match(stripped)
        if m:
            _flush()
            current_key = m.group(1)
            current_name = None
            continue

        # ``name:`` property inside the current job (4-space indent).
        if current_key is not None and current_name is None:
            m2 = name_val_re.match(stripped)
            if m2:
                current_name = m2.group(1).strip()

    # End of file — flush the last job.
    _flush()

    if not jobs:
        raise ValueError("no jobs found under the jobs: block")

    return jobs


def load_ci_jobs(root: Path) -> dict[str, str]:
    """Read .github/workflows/ci.yml and return {job_key: context_name}."""
    path = root / ".github" / "workflows" / "ci.yml"
    try:
        text = path.read_text()
    except OSError as exc:
        print(f"ERROR: cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        return parse_ci_jobs(text)
    except ValueError as exc:
        print(f"ERROR: {path}: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Convention check
# ---------------------------------------------------------------------------

def check_name_equals_key(jobs: dict[str, str], *, path_label: str = "ci.yml") -> list[str]:
    """Return error lines for every job where name != key.

    Our convention: when a job sets ``name:``, that value must equal the job
    key so the ruleset context and the job key are always the same string.
    """
    errors: list[str] = []
    for key, context in jobs.items():
        if context != key:
            errors.append(
                f"  job '{key}' has name: '{context}' — must equal the key per project convention"
            )
    return errors


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

_RULESET_TEMPLATE = """\
{{
  "rules": [
    {{
      "type": "required_status_checks",
      "parameters": {{
        "required_status_checks": [
          {checks}
        ]
      }}
    }}
  ]
}}
"""


def _make_ruleset(contexts: list[str]) -> str:
    checks = ", ".join(f'{{"context": "{c}"}}' for c in contexts)
    return _RULESET_TEMPLATE.format(checks=checks)


def _make_ci(jobs: list[tuple[str, str | None]]) -> str:
    """Build a minimal ci.yml.  Each job is (key, name_or_None)."""
    lines = ["jobs:"]
    for key, name in jobs:
        lines.append(f"  {key}:")
        if name is not None:
            lines.append(f"    name: {name}")
        lines.append(f"    runs-on: ubuntu-latest")
    return "\n".join(lines) + "\n"


def self_test() -> int:  # noqa: PLR0912  (many assertions is fine for a self-test)
    """Run inline assertions.  Returns 0 on success, 1 on any failure."""
    failures: list[str] = []

    def assert_eq(label: str, got: object, want: object) -> None:
        if got != want:
            failures.append(f"FAIL [{label}]: got {got!r}, want {want!r}")

    def assert_in(label: str, needle: str, haystack: str) -> None:
        if needle not in haystack:
            failures.append(f"FAIL [{label}]: {needle!r} not found in output")

    # ------------------------------------------------------------------
    # parse_ci_jobs: job with name equal to key
    # ------------------------------------------------------------------
    yaml1 = _make_ci([("api", "api"), ("mobile", "mobile")])
    jobs1 = parse_ci_jobs(yaml1)
    assert_eq("name==key: api context", jobs1.get("api"), "api")
    assert_eq("name==key: mobile context", jobs1.get("mobile"), "mobile")
    errors1 = check_name_equals_key(jobs1)
    assert_eq("name==key: no convention errors", errors1, [])

    # ------------------------------------------------------------------
    # parse_ci_jobs: job with name different from key → convention error
    # ------------------------------------------------------------------
    yaml2 = _make_ci([("api", "API Tests"), ("mobile", "mobile")])
    jobs2 = parse_ci_jobs(yaml2)
    assert_eq("name!=key: api context is name value", jobs2.get("api"), "API Tests")
    assert_eq("name!=key: mobile context is key", jobs2.get("mobile"), "mobile")
    errors2 = check_name_equals_key(jobs2)
    assert_eq("name!=key: one convention error", len(errors2), 1)
    assert_in("name!=key: error mentions job key", "api", errors2[0])
    assert_in("name!=key: error mentions name value", "API Tests", errors2[0])

    # ------------------------------------------------------------------
    # parse_ci_jobs: job without name → key is used as context
    # ------------------------------------------------------------------
    yaml3 = _make_ci([("secrets", None), ("api", None)])
    jobs3 = parse_ci_jobs(yaml3)
    assert_eq("no-name: secrets context == key", jobs3.get("secrets"), "secrets")
    assert_eq("no-name: api context == key", jobs3.get("api"), "api")
    errors3 = check_name_equals_key(jobs3)
    assert_eq("no-name: no convention errors", errors3, [])

    # ------------------------------------------------------------------
    # parse_ci_jobs: indentation robustness — deeper properties ignored
    # ------------------------------------------------------------------
    yaml4 = textwrap.dedent("""\
        jobs:
          deploy:
            name: deploy
            runs-on: ubuntu-latest
            steps:
              - name: checkout
                uses: actions/checkout@abc
              - name: run
                run: echo hi
        """)
    jobs4 = parse_ci_jobs(yaml4)
    assert_eq("indent-robust: only one job", set(jobs4.keys()), {"deploy"})
    assert_eq("indent-robust: context is name", jobs4.get("deploy"), "deploy")

    # ------------------------------------------------------------------
    # main() with mismatched ruleset → exit 1
    # ------------------------------------------------------------------
    import os, tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        scripts_dir = root / "scripts"
        scripts_dir.mkdir()
        (root / ".github" / "workflows").mkdir(parents=True)

        # ci.yml has jobs api + mobile; ruleset has api + extra
        ci_text = _make_ci([("api", "api"), ("mobile", "mobile")])
        ruleset_text = _make_ruleset(["api", "extra"])
        (root / ".github" / "workflows" / "ci.yml").write_text(ci_text)
        (scripts_dir / "github-ruleset.json").write_text(ruleset_text)

        import io, contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _real_main(root)
        out = buf.getvalue()
        assert_eq("mismatch: exit code 1", rc, 1)
        assert_in("mismatch: reports extra in ruleset", "extra", out)
        assert_in("mismatch: reports missing mobile", "mobile", out)

    # ------------------------------------------------------------------
    # main() with matching sets → exit 0
    # ------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / ".github" / "workflows").mkdir(parents=True)
        ci_text = _make_ci([("api", "api"), ("mobile", "mobile")])
        ruleset_text = _make_ruleset(["api", "mobile"])
        (root / ".github" / "workflows" / "ci.yml").write_text(ci_text)
        (root / "scripts" / "github-ruleset.json").write_text(ruleset_text)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _real_main(root)
        assert_eq("match: exit code 0", rc, 0)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------
    if failures:
        for f in failures:
            print(f, file=sys.stderr)
        print(f"ruleset-check --self-test: {len(failures)} failure(s)", file=sys.stderr)
        return 1

    print(f"ruleset-check --self-test: all assertions passed")
    return 0


# ---------------------------------------------------------------------------
# Main logic (extracted so the self-test can call it with a temp root)
# ---------------------------------------------------------------------------

def _real_main(root: Path) -> int:
    contexts = load_ruleset_contexts(root)
    jobs = load_ci_jobs(root)

    # Enforce convention: name: (when present) must equal the job key.
    convention_errors = check_name_equals_key(jobs)
    if convention_errors:
        print("ruleset-check: CONVENTION VIOLATION in .github/workflows/ci.yml")
        print("  Each job's name: must equal its key (GitHub uses name: as the status-check")
        print("  context; keeping them identical prevents silent drift with the ruleset).")
        for err in convention_errors:
            print(err)
        return 1

    # GitHub uses name: as the context; compare that set to the ruleset.
    context_names = set(jobs.values())

    only_ruleset = contexts - context_names
    only_ci = context_names - contexts

    if not only_ruleset and not only_ci:
        print(
            f"ruleset-check: OK — required_status_checks {sorted(contexts)} "
            f"match CI job contexts {sorted(context_names)}"
        )
        return 0

    print("ruleset-check: MISMATCH between scripts/github-ruleset.json and .github/workflows/ci.yml")
    if only_ruleset:
        print(
            f"  in ruleset but not in ci.yml: {sorted(only_ruleset)}\n"
            "  → add these as job name: values to ci.yml or remove them from the ruleset"
        )
    if only_ci:
        print(
            f"  in ci.yml but not in ruleset: {sorted(only_ci)}\n"
            "  → add these job context names to scripts/github-ruleset.json required_status_checks "
            "or remove the jobs from ci.yml"
        )
    print(
        "  NOTE: job contexts are frozen — the ruleset enforces them on every PR once enabled. "
        "See docs/plans/T0.2-plan.md and T6.7."
    )
    return 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        return self_test()

    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent
    return _real_main(root)


if __name__ == "__main__":
    sys.exit(main())
