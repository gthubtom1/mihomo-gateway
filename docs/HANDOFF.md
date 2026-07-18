# Project Handoff

## Project

Mihomo Gateway installs an authenticated public SOCKS5 gateway with a MetaCubeXD management panel.

## Current State

- Provider URL imports prefer a browser UA before client UAs, cap downloads at 16 MiB, and reject private/reserved targets and redirects before changing runtime config.
- Clash/Mihomo YAML is validated directly. Other common subscription formats use pinned Sub-Store `proxy-utils` 2.36.7 in a no-network, `nobody` child process and are validated as Mihomo YAML after conversion.
- Imported content is persisted as Mihomo's local provider cache. New managed providers retain the original URL in `x-source-url` and refresh through an authenticated `DIRECT` loopback endpoint so every update repeats safe fetch and conversion.
- The SOCKS5 tab can upload local YAML files as static providers. Uploads are size-limited, normalized, atomically written, and rolled back on validation or restart failure.
- Provider and SOCKS mutations are serialized, written atomically, and rolled back on validation/restart or cache-removal failure.
- Orphan YAML files, including non-ASCII and whitespace filenames, have opaque IDs and can be deleted exactly after backup.
- Provider API responses redact subscription paths, queries, and tokens; the panel renders provider-controlled values with DOM text nodes.
- Fresh installs no longer create the legacy `custom.yaml` provider. Reinstalls back up and remove stale provider YAML before optional imports.
- `SUB_URLS` imports run through the live management API after services start, using the same validation path as the panel.
- `bootstrap.sh` downloads the complete repository archive for the documented curl-pipe one-click install.
- Installer Nginx setup no longer kills unrelated port owners or removes unrelated sites.
- Every managed SOCKS5 listener has a distinct, fixed healthy primary node. Primary assignments are not reused, and creating or migrating listeners fails when there are not enough eligible primaries or no independent backup.
- Managed listener groups use OpenAI's unauthenticated `401` response as their health signal every 60 seconds. An unhealthy primary automatically falls through to another eligible provider node, and generated groups contain no `DIRECT` route.
- Existing listeners can be migrated in place while preserving their ports, names, credentials, source filters, and runtime-selected source groups. Re-running migration never reassigns an already-managed primary; temporary failures are handled by Mihomo's fallback and recover back to the fixed primary.
- Existing installations use a backed-up in-place upgrade. Runtime config, providers, panel files, CLI files, and converter files are restored when an upgrade or migration fails.
- The MetaCubeXD SOCKS5 tab shows each listener's primary and routing mode, supports migration, remains active after refresh, and stacks management inputs on narrow screens.

## Verification

- `python -m unittest discover -s tests -v`: 94 tests passing locally.
- `python -m py_compile panel/app.py scripts/render-config.py tests/*.py`: passing.
- `bash -n bootstrap.sh install.sh uninstall.sh scripts/common.sh scripts/mihomo-gateway`: passing with Git Bash.
- Extracted injected JavaScript parses with Node.js.
- `git diff --check`: passing.
- Gitleaks 8.30.1 reports zero findings in the working tree and all 20 Git commits; custom IPv4, URL-host, and credential-pattern review found only loopback, documentation placeholders, and test fixtures.
- Subscription HTTP 429 responses now stop immediate UA retries and retain `Retry-After`. When direct requests remain 403/429 and a cached provider already exists, imports retry through the local authenticated SOCKS listener while still connecting to the validated public IP.
- Real VPS `mihomo -t` validation passed, and Mihomo, the management API, and Nginx are active.
- Five migrated listeners retain their original identities and provider data while using five unique primary assignments and five distinct public exits. After a health cycle, every listener reached the OpenAI API and received the expected `401` response.
- Browser verification passed on desktop and a 390 px viewport. The SOCKS5 page survives reload on its canonical MetaCubeXD route, narrow form controls do not overflow, and a clean browser tab reports zero console errors.
- Independent read-only review found and verified fixes for provider-scoped node identity, managed provider-scope preservation, `select + use` runtime selection, fixed-primary idempotence, and complete UI-directory rollback. The follow-up review found no remaining Critical, High, or Medium issues.

## Pending

- Configure HTTPS or restrict the panel network path before treating management credentials as protected in transit.
- Pin/audit the Mihomo, MetaCubeXD, and repository refs for a reproducible release; defaults currently track upstream/latest branches for one-click updates.

## Next Session First Step

Run the post-update health check, confirm all managed listeners still report distinct healthy primaries, and inspect service logs before the next release.
