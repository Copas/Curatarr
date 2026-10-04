# 007: Sign in with Jellyfin administrator accounts

The owner prefers Jellyfin accounts over separate Curatarr accounts, matching how Seerr works. Spec section 90 prefers local accounts "or a clearly isolated auth adapter". All Jellyfin-specific sign-in code lives in `curatarr/auth.py`, so it is that isolated adapter.

Jellyfin has no permission dedicated to editing configuration; the Administrator policy grants dashboard access. Curatarr therefore requires `Policy.IsAdministrator` and refuses disabled accounts. Non-administrators cannot sign in at all, rather than getting read-only access, because the Review queue can approve deletions.

Credentials go to `POST /Users/AuthenticateByName` from a client without the stored API key. The returned Jellyfin token is revoked immediately with `POST /Sessions/Logout`. Curatarr stores only the user ID, display name, and last verification time in its signed session cookie. Every five minutes the account is re-read with Curatarr's API key (`GET /Users/{id}`). Losing administrator status, being disabled, or a 404 ends the session. Other failures allow up to one hour since the last successful check, so a short Jellyfin outage does not lock the operator out. A longer outage requires signing in again.

On first run the sign-in page accepts the Jellyfin server URL, and the first successful administrator sign-in saves it as the Jellyfin integration. This mirrors Seerr's setup but means the first visitor chooses the server. `CURATARR_JELLYFIN_URL` closes that window.

Failed sign-ins are limited to five per client address per 15 minutes, held in process memory. That is sufficient for the single-worker deployment; with multiple workers or behind a proxy it is coarser.

`/health`, `/api/v1/status`, and the token-checked webhook stay public, because dashboards and Jellyfin call them without a browser session. `CURATARR_ALLOW_UNAUTHENTICATED=true` disables sign-in for deployments where an authenticating reverse proxy already controls access.
