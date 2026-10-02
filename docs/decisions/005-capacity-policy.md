# 005: Capacity selection uses explicit sizes and paths

Library quotas use measured file sizes and high/low watermarks. Disk-pressure selection requires a configured arr-side path that matches a disk-space record from the relevant arr service. Missing or ambiguous information suppresses selection. Immediately before deletion, Curatarr re-ranks capacity candidates, so later favorite or Never Purge changes can invalidate an earlier decision.
