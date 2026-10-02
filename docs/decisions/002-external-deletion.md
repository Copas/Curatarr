# 002: Delete only through arr APIs

Curatarr never touches media files directly. TV collapse deletes Sonarr episode files outside the retained footprint; movie deletion uses Radarr's movie delete operation with file deletion enabled and import exclusion disabled. A dry run and fresh pre-delete validation precede every destructive request.

