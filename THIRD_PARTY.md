# Third-Party Runtime Components

## Sub-Store proxy-utils

- Project: `sub-store-org/Sub-Store`
- Component: `proxy-utils.esm.mjs`
- Version: `2.36.7`
- License: MIT
- Install source: GitHub release asset
- SHA256: `fede079cf5f67e095c3d6e858851a7d6fa6e92954be5fa3acbfe9e48a9a71a3d`

The installer downloads and verifies this fixed asset. It is not committed into this repository. At runtime it receives only already-downloaded subscription content through stdin and runs as `nobody` in a separate network namespace.

## Node.js

- Project: Node.js
- Version: `20.19.5`
- Install source: official `nodejs.org` release tarball
- Architectures: x64, arm64, armv7l
- Integrity: architecture-specific SHA256 values pinned in `scripts/common.sh`

Only the verified `node` binary is installed under `/opt/mihomo-gateway/node/`; the system Node package and package-manager configuration are not changed.

## Mihomo and MetaCubeXD

Mihomo and MetaCubeXD are installed from their upstream projects. Their current installer paths still follow upstream release/default references; see `docs/SECURITY.md` for the reproducibility limitation.
