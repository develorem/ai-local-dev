# 05 — Distribution & billing (decided)

## Two ways to get OrchestrAi
1. **Self-hosted** — a one-command `docker compose` bundle (single-tenant).
   - Pricing: one-off cost + optional support subscription. **Exact prices TBD**
     (fine to defer — A2). Update/upgrade policy also TBD (A4).
   - **Purchase is gated:** the user signs up on the cloud site, pays, and then
     receives a **license + download link**. No download before purchase (A5).
2. **Cloud-hosted** — we operate it on Cloudflare (multi-tenant). Plans:
   - **Free** — own org; join others; up to 2 projects; 1 agent; no invites.
     (The Free tier is the no-cost entry; no separate timed trial at launch.)
   - **Pro $4.95/mo** — up to 10 projects; unlimited own agents; invites; unlocks
     leased agents.
   - **Team $19.90/mo** — unlimited projects/members/agents.
   - **Stripe** (checkout + customer portal + webhooks, carried from v1). Annual
     billing / other options later.

## Licensing (self-hosted)
- **Signed license key**, **verifiable offline** (public-key verify; no phone-home
  required) so **fully air-gapped, internet-isolated deployments** work — explicit
  requirement for ultra-secure environments.
- The key encodes **entitlements + an expiry** (expiry support is wanted).
- An *optional* online check may refresh the model catalog / updates when the box
  has connectivity, but nothing required is gated on it.

## Leased (cloud-hosted) agents — designed-for, built later
Cloud-only, Pro+. Customer adds N hosted agents in the portal + pays; we provision
them (e.g. Cloudflare Containers running a local-LLM image like openclaw) and
connect them to the customer's tenant as ordinary executors. Not built now; the
contract + data model must accommodate it. Not offered for self-hosted.

## Carried-forward
- Self-hosted price points + what "support" includes + update/upgrade policy.
- License key library/format (and how expiry + revocation are handled offline).
