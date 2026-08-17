# Contributing to GPA

Thank you for helping GPA learn from real work. Contributions can be code,
documentation, Replay examples, compatibility evidence, design critique or a
carefully reduced failure report.

## Before opening anything

- Search existing Issues and Discussions.
- Remove credentials, customer information, private screenshots, browser
  sessions and API responses from logs or recordings.
- Use synthetic or public data when reducing a workflow.
- For a vulnerability or privacy leak, follow `SECURITY.md` instead of opening
  a public issue.

## Choose the right path

- Bug: use the Bug report issue form with a minimal reproduction.
- New workflow: use the Real-world workflow form. Explain the business outcome,
  not only the clicks.
- Cross-machine failure: use the Compatibility report form.
- Product idea or question: start a Discussion before a large implementation.
- Small code or documentation improvement: open a pull request directly.

## Development setup

```bash
git clone https://github.com/hanshenmesen/gpa.git
cd gpa
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[desktop,cloud,dev]"
cp .env.example .env
./start.sh --check --skip-install
```

Desktop automation is disabled by default. Do not enable it while running the
test suite or reviewing untrusted packages.

## Required checks

```bash
python -m ruff check .
python -m compileall -q gpa demo_web scripts
node scripts/verify_frontend.js
python -m unittest discover -s tests -p "test_*.py"
python -m build --wheel
python scripts/verify_distribution.py dist
```

Changes to safety gates, credentials, package parsing, recording, execution or
tenant isolation require tests for the refusal path as well as the success path.

## Pull requests

- Keep the purpose explicit and the diff reviewable.
- Describe user impact, risk, and verification.
- Link the Issue or Discussion when one exists.
- Add or update tests and user documentation.
- Do not mix generated artifacts, secrets or unrelated formatting changes.

By contributing, you agree that your contribution is licensed under the
Apache License 2.0 and that you have the right to submit it.
