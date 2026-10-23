# 004: Preserve original posters and track image versions

Curatarr stores original and badged poster bytes under application data, associated with one candidate. Badges are always generated from the original source. The snapshot and source bytes are committed before the image upload, so a lost upload response does not leave an untracked badge.

Jellyfin exposes a primary image tag on item data. Curatarr records the tag after a confirmed badge upload and restores only when the current tag still matches that badge, or when the served bytes exactly match the uploaded badge. The tag allows restoration when Jellyfin re-encodes served image bytes. If the original image is already present, the snapshot closes without another upload. If the tag and bytes indicate a different external edit, restoration stops to preserve that edit. An ambiguous upload with re-encoded bytes and no recorded tag still requires manual review.

The tag is treated as Jellyfin's image version, not as a content hash; compatibility with specific Jellyfin versions still needs live validation. See [Jellyfin's item DTO](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Model/Dto/BaseItemDto.cs) for `ImageTags` and [Jellyfin Web's image handling](https://github.com/jellyfin/jellyfin-web/blob/master/src/utils/jellyfin-apiclient/backdropImage.ts) for its use of `ImageTags.Primary`.

## Update 2026-10: compare pictures, not tags

Jellyfin changes an uploaded image's tag a while after the upload. Read as an external edit, that saved badged pictures as originals and badged them again (over 1,400 uploads a day on one server). Curatarr now compares the picture it serves with the stored badged copy before treating artwork as replaced. Snapshots whose saved original may itself be badged are marked suspect; restoring or repairing one has Jellyfin download the artwork afresh from its providers instead, which replaces any hand-picked poster with the provider's choice. Plugins that change posters on a schedule (poster rotators) look like constant external edits and should not run alongside Leaving Soon badges.

## Badges in dry run

Leaving Soon badges are applied whether or not a library is in dry run. The owner decided on 2026-10-04 that dry run exists to rehearse the whole visible process, including what viewers see on posters. Only the final Sonarr or Radarr deletion request is withheld.
