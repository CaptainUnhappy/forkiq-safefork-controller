# ForkIQ
<!-- impeccable:product-schema 1 -->

## Platform
web

## Users
Administrators managing their GitHub forks from one private panel.

## Product Purpose
Keep forks current with upstream and make synchronization failures actionable.

## Capabilities and Constraints
FastAPI and server-rendered Jinja templates. GitHub App authentication, discovery,
mirror synchronization, history-preserving upstream merges, optional workflow
keepalive, repository controls, events, per-repository local/upstream version
display (latest tag, short SHA, commit date) and administrator settings already exist.
The redesign preserves all server actions, CSRF protections, password-only panel
login, existing credentials and repository configuration. No real credentials
belong in source, fixtures or documentation.

## Brand Commitments
Keep the ForkIQ name and Chinese interface. The user requests the visual language
of https://kr.hx10.com:3000/#agents, including its centered login page: light
neutral surfaces, white navigation and green actions.

## Product Principles
- Preserve fork history and expose errors rather than hiding them.
- Make repository state and available actions easy to scan.
- Keep desktop and mobile workflows usable without changing synchronization.
