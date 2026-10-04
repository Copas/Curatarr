# 005: Capacity selection uses explicit sizes and paths

Library quotas use measured file sizes and high/low watermarks. Disk-pressure selection requires a configured arr-side path that matches a disk-space record from the relevant arr service. Missing or ambiguous information suppresses selection. Immediately before deletion, Curatarr re-ranks capacity candidates, so later favorite or Never Purge changes can invalidate an earlier decision.

## Quota hysteresis

A quota run starts when stored bytes exceed the high-water mark and continues until the low-water target is reached. Between the two marks, queued quota candidates stay valid only if Curatarr itself has completed a quota deletion in that library since the candidate was created. If the library drops below high water for any other reason, such as an external deletion, queued candidates are blocked before deletion and rescued at the next reconciliation.

## Disks Sonarr and Radarr do not report

Sonarr and Radarr leave network mounts out of their disk-space reports. On the owner's host, the CIFS mount holding every library (`/mnt/zeno_movies_tv`) is missing, and only `/` and `/mnt/replaceable` are listed. Prefix matching then picked `/`, the local drive, for NAS library paths. Now the `/` record matches only a configured path of `/`. If no other record matches and the configured path is visible on Curatarr's own host (same-machine installs), free space is measured directly with `statvfs`. Otherwise selection is suppressed, as before.

## One selection per disk

Free-space pressure belongs to a disk, not a library. Libraries are grouped by the disk they actually sit on: the matching arr record, or the device ID when measured directly. The shortfall is computed once per disk, using the largest shortfall among its libraries, and titles are chosen from the combined pool until it is covered. A lone library keeps its own purge strategy. A shared pool is ordered oldest-activity-first with favorites last. The check just before deletion uses the same shared selection.
