# GPA public feedback program

## Goal

Collect evidence about whether GPA can turn real desktop work into a safe,
understandable and portable Replay. Download counts and stars are useful reach
signals, but they are not product success by themselves.

## Feedback channels

| Signal | GitHub channel | Required evidence |
| --- | --- | --- |
| Reproducible defect | Bug report Issue | revision, safe environment, minimal steps |
| New real task | Real-world workflow Issue | outcome, frequency, apps, success criteria, risk |
| Cross-machine result | Compatibility Issue | source/target facts, preflight decision, failing step |
| Early product idea | Discussion / Ideas | user problem, current workaround, safety boundary |
| Successful example | Discussion / Show and tell | outcome, lessons, safe sharing method |
| Vulnerability/privacy leak | Private advisory | impact and minimal private reproduction |

## Triage rubric

Each report is evaluated on five dimensions:

1. Real-world frequency and time saved.
2. Reproducibility with public or synthetic data.
3. Safety and reversibility of the requested behavior.
4. Cross-environment learning value.
5. Ability to verify the final business outcome.

High-risk actions are not prioritized merely because they are popular. Missing
privacy consent or unredacted data blocks public triage.

## Labels and lifecycle

`needs-triage` → `needs-evidence` or accepted area label → implementation PR →
`needs-verification` → reporter validation → closed in a prerelease milestone.

Recommended area labels: `recording`, `replay`, `compatibility`, `safety`,
`community`, `cloud`, `ui`, `documentation`, and `real-world-case`.

## Public response standard

- Acknowledge useful reports and say what evidence is missing.
- Never ask a user to share credentials, customer data or raw session state.
- Explain safety refusals concretely.
- Link fixes to the originating report and ask the reporter to verify.
- Summarize repeated reports into a single canonical Issue.
- Publish roadmap changes with the evidence that changed the decision.

## What to measure

- Time from report to reproducible case.
- Percentage of accepted cases with deterministic success criteria.
- Source-to-target compatibility pass/block/unknown rates.
- False-safe and false-block preflight reports.
- Emergency-stop reliability and refusal-path regressions.
- Recording compression: raw events, final semantic steps, merged actions and
  removed noise.
- Reporter-confirmed fixes per prerelease.

Do not collect raw screen recordings or screenshots as default telemetry.
Metrics should be local or explicitly submitted, redacted and scoped.
