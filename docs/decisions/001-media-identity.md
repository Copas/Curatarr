# 001: Match media with stable provider IDs

Jellyfin item IDs may change on rescan. Curatarr stores both item IDs and provider IDs, and uses a unique provider match within a library when the item ID no longer matches. Ambiguous matches are never silently merged. Title text is display data only.

