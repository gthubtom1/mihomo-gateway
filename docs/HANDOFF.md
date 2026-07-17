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

- `python -m unittest discover -s tests -v`: 47 tests passing.
- `python -m py_compile panel/app.py scripts/render-config.py`: passing.
- `bash -n bootstrap.sh install.sh uninstall.sh scripts/common.sh scripts/mihomo-gateway`: passing with Git Bash.
- Extracted injected JavaScript parses with Node.js.
- `git diff --check`: passing.
- Two independent read-only reviews completed; confirmed findings for SOCKS XSS, DNS rebinding, UFW rollback, secret handling, and reinstall backup were fixed and covered by tests.
- Subscription HTTP 429 responses now stop immediate UA retries and retain `Retry-After`. When direct requests remain 403/429 and a cached provider already exists, imports retry through the local authenticated SOCKS listener while still connecting to the validated public IP.

## Pending

- GitHub repository `gthubtom1/mihomo-gateway` is public and `main` is deployed through commit `963dd83`.
- The target VPS was backed up and installed through the public curl-pipe bootstrap. Existing Nginx and Docker services remained active.
- Two local YAML files were normalized into static providers with 171 and 20 nodes. AUTO has 191 candidates and GPT has 60; neither automatic group selects `DIRECT` while providers exist.
- Authenticated SOCKS egress uses an airport exit, temporary SOCKS create/delete passed, the public panel returned the injected UI, and a GPT-routed request reached the target HTTP service.
- Replace the static providers with panel-managed subscription URLs when the operator has the original URLs; static YAML cannot update itself.
- Configure HTTPS or restrict the panel network path before treating management credentials as protected in transit.
- Pin/audit the Mihomo, MetaCubeXD, and repository refs for a reproducible release; defaults currently track upstream/latest branches for one-click updates.

## Next Session First Step

Obtain the two original subscription URLs, add them in the panel, verify node counts, then delete the matching static providers so future airport changes update automatically.
