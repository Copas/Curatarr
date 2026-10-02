# 004: Preserve original poster bytes

Curatarr stores original and badged poster bytes under application data, associated with one candidate. Badges are always generated from the original source. Restoration only proceeds when Jellyfin still returns the exact badged bytes Curatarr uploaded. This favors preserving a third party's later artwork change over forcing restoration; Jellyfin image re-encoding can leave a badge that requires manual attention.

