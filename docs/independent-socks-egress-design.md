# Independent SOCKS5 Egress Design

Date: 2026-07-18

## Goal

Each managed SOCKS5 listener should keep a distinct primary provider node when enough eligible nodes exist. If that node becomes unhealthy or disappears, Mihomo should automatically use another healthy node without changing the listener credentials or port.

## Routing Model

Each independent listener owns three hidden proxy groups derived from its selected source group:

- `MGW-<port>-PRIMARY`: a `url-test` group filtered to one exact provider node.
- `MGW-<port>-BACKUP`: a `fallback` group containing the same eligible provider set while excluding the primary node.
- `MGW-<port>`: a `fallback` group ordered as primary, then backup. The listener points here.

Provider health checks call the OpenAI models endpoint every 60 seconds, treat its expected `401` response as healthy, and disable lazy checking. The outer group returns to the primary after it becomes healthy again. Existing source group filters and provider scope are copied so a GPT or region endpoint does not escape its intended node set.

## Allocation

Provider cache files are read in configured provider and node order, then intersected with Mihomo's provider-scoped live health state. Node identity is the `(provider, node name)` pair, and each primary group references only that provider, so equal names from different providers remain distinct physical candidates. A new or migrated listener receives the first eligible healthy identity that is not already a primary for another independent listener. At least two healthy candidates with a usable differently named backup are required. If no unassigned healthy primary remains, creation or migration is rejected without changing the existing listener.

The selected primary is represented by the generated primary group's exact regular-expression filter. No extra secret or sidecar state is required. If a provider refresh removes that node, the primary group becomes unavailable and the backup group remains dynamic.

## Migration

Migration is an authenticated, explicit mutation:

1. Back up the current Mihomo config through the existing atomic save path.
2. Process existing SOCKS listeners in port order.
3. Preserve authentication, port, name, firewall rules, and source group intent.
4. Resolve top-level select groups through Mihomo's current runtime selection.
5. Replace only each listener's route with generated managed groups.
6. Validate the complete Mihomo config and restart once.
7. Restore the original config and restart if validation or startup fails.

Already-managed listeners are idempotent and are not reassigned during later migrations.

## Lifecycle

- Creating a listener uses independent routing by default.
- Deleting a listener removes only its three generated groups.
- Provider add does not expand existing generated groups into a new provider. Provider delete removes only the deleted provider reference and leaves the generated route structure intact.
- Listing listeners returns source group, primary node, independent mode, and route group for the panel.
- The panel offers an explicit migration action for pre-existing listeners and displays the assigned primary.

## Safety And Limits

- Creation fails before writing config when no eligible provider node exists.
- Generated regexes use `re.escape` and generated group names use validated numeric ports.
- `DIRECT` is never selected as a primary or backup for independent endpoints.
- Empty provider-backed groups use `REJECT`, including legacy groups after their last provider is deleted.
- Generated groups use Mihomo's `empty-fallback: REJECT`, so a provider that still exists but has no matching nodes cannot fall through to `COMPATIBLE`.
- An unavailable Mihomo health API aborts the mutation instead of silently assigning stale nodes.
- Existing providers, provider cache files, subscriptions, listener credentials, and firewall entries are not deleted by migration.
- Existing installations take the in-place upgrade path; config, providers, credentials, ports, and UI are backed up and restored if upgrade or migration fails.
- Primary nodes are never reused across managed listeners.

## Verification

- Unit tests cover distinct assignment, source-filter preservation, automatic group structure, deletion cleanup, idempotent migration, node reuse, no-node rejection, and rollback.
- Panel rendering tests cover migration controls and independent route status.
- Mihomo validates the generated config on Linux before deployment.
- Live verification checks service health, distinct primary assignments, and an authenticated request through every migrated SOCKS listener.
