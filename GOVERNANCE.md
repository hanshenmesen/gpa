# Project governance

GPA uses a maintainer-led, evidence-driven model during the technical preview.

## Decision principles

1. User safety and reversibility outrank automation success rate.
2. Real cross-machine evidence outranks demo-only behavior.
3. Local authority and user data ownership are architectural constraints.
4. Stable, inspectable contracts outrank hidden heuristics.
5. Maintainers document material trade-offs in Issues, Discussions or `DESIGN.md`.

## Roles

- Contributors submit evidence, code, documentation and review.
- Maintainers triage, review, merge, release and moderate community spaces.
- Security reporters use the private advisory process.

Maintainers may close unsafe workflow requests, remove sensitive uploads, or
hold a change until refusal-path tests and documentation are complete. Project
direction is discussed publicly when doing so does not expose a vulnerability
or private data.

## Changes to governance

Governance changes use a pull request with a public rationale and a reasonable
discussion window. The repository owner has final responsibility during the
technical preview; broader maintainer succession will be defined as sustained
contributors emerge.
