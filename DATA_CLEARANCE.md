# Data Clearance — CFS (Community Forcing Service)

Per-provider licensing clearance for commercial use and redistribution. **This documents the terms of third-party data sources; it does not grant any rights to that data.** The CFS *code* is licensed separately (see `LICENSE`); using CFS to acquire data does not transfer any rights in the data itself. You are responsible for complying with each source's terms. Machine-readable detail: [`inventory/clearance.csv`](inventory/clearance.csv).

## Two-axis model

- **commercial_use** — may an *end user* use the data for commercial purposes?
- **redistribution** — may a *third party re-host/re-serve* it? (`conditional` = yes with attribution/share-alike)

## Tiers

| Tier | Meaning | Self-hosted client (commercial) | Hosted SaaS (redistribution) |
|---|---|---|---|
| A | Public domain / open | ✅ | ✅ |
| B | Attribution required | ✅ (attribute) | ✅ (attribute) |
| B-SA | Attribution + share-alike | ✅ | ⚠️ derived data inherits copyleft |
| C | Non-commercial / research-only | 🔴 gate out | 🔴 gate out |
| D | No redistribution / gated | user-BYO only | 🔴 never serve |
| E | Unknown — unverified | ⚠️ treat as restricted | 🔴 until cleared |

## CFS summary (36 providers)

| A | B | B-SA | C | D | E |
|--|--|--|--|--|--|
| 23 | 12 | 0 | 1 | 0 | 0 |

**Commercial-clearable (A/B): 35/36.** Gate from commercial use: 1 C + 0 E. Never host: 0 D.

## Restricted / unverified providers (do not auto-clear)

| Tier | Provider | License | Why |
|---|---|---|---|
| C | MSWEP (Multi-Source Weighted-Ensemble Precipitation, 0.1°) | non-commercial/research | non-commercial |

## Method

Each provider's free-text licence was normalized to the two-axis schema and tier; values marked `agent-verified` in `clearance.csv` were confirmed against the official licence page (see the `source` column). Tiers are derived deterministically; re-running the classifier preserves verified rows. `E` rows have no publishable terms and require a direct request to the source agency or a legal determination.
