# 005: Capacity selection uses explicit sizes and paths

Library quotas use measured file sizes and high/low watermarks. Disk-pressure selection requires a configured arr-side path that matches a disk-space record from the relevant arr service. Missing or ambiguous information suppresses selection. Immediately before deletion, Curatarr re-ranks capacity candidates, so later favorite or Never Purge changes can invalidate an earlier decision.

## Quota hysteresis

A quota run starts when stored bytes exceed the high-water mark and continues until the low-water target is reached. Between the two marks, queued quota candidates stay valid only if Curatarr itself has completed a quota deletion in that library since the candidate was created. If the library drops below high water for any other reason, such as an external deletion, queued candidates are blocked before deletion and rescued at the next reconciliation.
