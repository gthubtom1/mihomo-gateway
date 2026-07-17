# Project Handoff

## Project

Mihomo Gateway installs an authenticated public SOCKS5 gateway with a MetaCubeXD management panel.

## Current State

- Provider URL imports retry client user agents, validate Clash/Mihomo YAML, cap downloads at 16 MiB, and reject private/reserved targets and redirects before changing runtime config.
- Imported provider content is persisted as Mihomo's local provider cache while the original stable subscription URL is retained for refreshes.
- Provider and SOCKS mutations are serialized, written atomically, and rolled back on validation/restart or cache-removal failure.
- Orphan YAML files, including non-ASCII and whitespace filenames, have opaque IDs and can be deleted exactly after backup.
- Provider API responses redact subscription paths, queries, and tokens; the panel renders provider-controlled values with DOM text nodes.
- Fresh installs no longer create the legacy `custom.yaml` provider. Reinstalls back up and remove stale provider YAML before optional imports.
- `SUB_URLS` imports run through the live management API after services start, using the same validation path as the panel.
- `bootstrap.sh` downloads the complete repository archive for the documented curl-pipe one-click install.
- Installer Nginx setup no longer kills unrelated port owners or removes unrelated sites.

## Verification

- `python -m unittest discover -s tests -v`: 36 tests passing.
- `python -m py_compile panel/app.py scripts/render-config.py`: passing.
- `bash -n bootstrap.sh install.sh uninstall.sh scripts/common.sh scripts/mihomo-gateway`: passing with Git Bash.
- Extracted injected JavaScript parses with Node.js.
- `git diff --check`: passing.
- Two independent read-only reviews completed; confirmed findings for SOCKS XSS, DNS rebinding, UFW rollback, secret handling, and reinstall backup were fixed and covered by tests.

## Pending

- GitHub private repository `gthubtom1/mihomo-gateway` exists and `main` is pushed through commit `e6f86c6`.
- Decide whether to make the repository public. Anonymous curl-pipe install cannot use the private raw URL.
- Back up the live VPS config/provider directory before replacing files or rerunning the installer.
- Deploy only when SSH/HTTP connectivity returns, then verify a real subscription import, node count, exact deletion, SOCKS create/delete, and authenticated egress.
- Confirm the one-click command from a clean Linux environment after the pushed `bootstrap.sh` is available remotely.
- Configure HTTPS or restrict the panel network path before treating management credentials as protected in transit.
- Pin/audit the Mihomo, MetaCubeXD, and repository refs for a reproducible release; defaults currently track upstream/latest branches for one-click updates.

## Next Session First Step

Recheck the target VPS SSH and panel ports; the latest probes found both known VPS SSH ports unreachable and the target panel port timed out. Once reachable, back up first and run the live provider/SOCKS lifecycle verification before declaring deployment complete.
