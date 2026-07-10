# Security and Privacy Policy

Blybot's threat model includes both classic security issues **and privacy
regressions** — for this project, "a Telegram username reaches the wiki" is a
vulnerability, not a bug.

## Reporting

Please report vulnerabilities and privacy leaks **privately**:

- via GitHub's private vulnerability reporting on this repository
  ("Report a vulnerability" under the Security tab), or
- by email to the maintainer listed on the bot's Meta user page.

Please do not open public issues for these reports. You can expect an
acknowledgement within a week.

## Scope

In scope, in particular:

- sanitizer bypasses (any input whose publication alters page structure,
  transcludes, categorizes, or signs);
- any path where a Telegram identifier reaches the wiki, disk, or logs;
- pseudonym predictability or cross-session linkability;
- the bot processing group messages it should never receive.

## Supported versions

Only the latest release / `main` is supported.
