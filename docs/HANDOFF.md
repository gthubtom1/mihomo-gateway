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

## Verification

- `python -m unittest discover -s tests -v`: 66 tests passing on the target Linux VPS.
- `python -m py_compile panel/app.py scripts/render-config.py`: passing.
- `bash -n bootstrap.sh install.sh uninstall.sh scripts/common.sh scripts/mihomo-gateway`: passing with Git Bash.
- Extracted injected JavaScript parses with Node.js.
- `git diff --check`: passing.
- Two independent read-only reviews completed; confirmed findings for SOCKS XSS, DNS rebinding, UFW rollback, secret handling, and reinstall backup were fixed and covered by tests.
- Subscription HTTP 429 responses now stop immediate UA retries and retain `Retry-After`. When direct requests remain 403/429 and a cached provider already exists, imports retry through the local authenticated SOCKS listener while still connecting to the validated public IP.

## Pending

- The multi-format converter is hot-deployed with pinned Node 20.19.5 and Sub-Store `proxy-utils` 2.36.7; local and remote SHA256 values match.
- Live validation passed for a managed 41-node provider refresh and for YAML upload/create/delete using a temporary one-node provider.
- Mihomo config validation, API/Mihomo/Nginx service health, AUTO/GPT exclusion of `DIRECT`, and authenticated GPT-routed SOCKS access to the OpenAI API all passed.
- A 171-node short-lived cache is retained but currently unhealthy: its endpoint ports work from the operator network and time out from the VPS after credential expiry. The supplied token now returns `429` from the VPS and `403` through the operator's proxy, so it cannot be refreshed without a new URL or YAML.
- Configure HTTPS or restrict the panel network path before treating management credentials as protected in transit.
- Pin/audit the Mihomo, MetaCubeXD, and repository refs for a reproducible release; defaults currently track upstream/latest branches for one-click updates.

## Next Session First Step

Import a fresh YAML or replace the expired short-lived URL, then verify the 171-node provider before removing its stale cache.
