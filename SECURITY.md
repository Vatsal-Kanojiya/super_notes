# Security policy

## Reporting a vulnerability

Please report security problems privately, through GitHub's **Report a vulnerability** button on
this repository's Security tab, rather than in a public issue. You should hear back within a week.

## Fixing vulnerable dependencies

This project depends on third-party packages (`requirements.txt`, `web/package.json`, the
GitHub Actions it uses). When one of them is found to have a known vulnerability, it is fixed
within these time frames, counted from when the advisory is published or first reported to us:

| Severity (CVSS / the advisory's own rating) | Fixed within |
|---|---|
| Critical | 7 days |
| High | 14 days |
| Medium | 30 days |
| Low | 90 days, or the next routine update |

"Fixed" means one of: upgraded to a release without the vulnerability; the vulnerable code shown
to be unreachable from this app, with the reasoning recorded in the pull request; or a
workaround in place until an upgrade exists.

### How they are found

- **Dependabot** (`.github/dependabot.yml`) checks the Python packages, the npm packages
  and the GitHub Actions every week and opens a pull request for each update, security updates
  immediately. CI runs on each of those pull requests.
- **`pip-audit -r requirements.txt`** is run before every deployment, and at least monthly.
- A failing audit, or an open security update from Dependabot past its time frame, blocks the
  next deployment until it is handled.

This is OWASP ASVS 5.0 requirement V15.1.1 (documented remediation time frames) and the
process behind V15.2.1 (components kept within them). The ASVS Level 2 posture this app keeps is summarised in `docs/CONVENTIONS.md` §6.
