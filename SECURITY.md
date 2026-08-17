# Security policy

GPA can observe screens and control desktop input when explicitly armed. Treat
security and privacy reports as product-critical.

## Supported versions

Only the latest commit on `main` and the latest GitHub prerelease receive
security fixes during the technical preview.

## Report privately

Use GitHub's **Report a vulnerability** / private security advisory flow for
this repository. Do not open a public Issue for:

- unintended keyboard, pointer, clipboard or screen access;
- bypass of local approval, emergency stop or protected-app controls;
- credential, cookie, screenshot, recording or environment leakage;
- package import leading to execution or path traversal;
- cross-tenant data access or authentication bypass;
- remotely supplied arbitrary commands;
- a dependency vulnerability with a demonstrated GPA impact.

Include affected revision, platform, impact, minimal reproduction and any safe
logs. Redact all third-party data and secrets. We will acknowledge a complete
report as soon as practical, coordinate remediation and credit reporters who
want attribution.

## Security expectations

- Importing a Replay must never execute it.
- Cloud jobs are proposals; only the installed app grants desktop authority.
- Desktop control remains disabled until explicitly enabled and armed.
- Raw screenshots and local credentials are not community artifacts.
- Public packages are untrusted until quarantined and inspected.
- Production endpoints use HTTPS/WSS and credentials belong in a secret store.

Unsigned technical-preview builds do not provide Developer ID or notarization
assurance. Verify release checksums and prefer source installation until signed
distribution is available.
