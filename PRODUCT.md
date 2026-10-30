# ForkIQ SafeFork Controller
<!-- impeccable:product-schema 1 -->

## Platform
web

## Users
Administrators managing their GitHub forks from one private panel.

## Product Purpose
Preserve upstream Git history without rewinding target branches, moving existing
tags, or executing upstream CI. Make blocked synchronization visible and reviewable.

## Capabilities and Constraints
FastAPI, server-rendered Jinja templates, SQLite, one serialized Git worker.
Explicit repository registration pins source and target IDs. Registration stays
paused until a read-only preview is confirmed. One safe write path handles all
heads and raw tags using normal atomic push. Public sources use public native
forks; authorized private sources use standalone private targets and separate
read credentials. Target Actions remain disabled and quiescent.
No discovery, merge, force, rollback, workflow keepalive, or Actions settings writes.
No real credentials or private repository contents belong in source or fixtures.

## Brand Commitments
Retain ForkIQ attribution and the Chinese interface: light neutral surfaces,
white navigation, green actions, and a centered login page.

## Product Principles
- Preserve fork history and expose errors rather than hiding them.
- Make repository state and available actions easy to scan.
- Show full-ref plans and the limits of previews, pause, and synchronization.
- Keep desktop and mobile workflows usable without hiding safety gates.
