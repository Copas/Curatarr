# 002: Delete only through arr APIs

Curatarr never touches media files directly. TV collapse deletes Sonarr episode files outside the retained footprint; movie deletion uses Radarr's movie delete operation with file deletion enabled and import exclusion disabled. A dry run and fresh pre-delete validation precede every destructive request.

TV collapse unmonitors each episode on a file just before deleting that file, and records the episode IDs in the action's `unmonitored` payload. A monitored episode without a file counts as missing to Sonarr, so without this the next missing-episode search would re-download the trimmed episodes. Going one file at a time means an interrupted collapse leaves the remaining episodes monitored and their files intact. When viewing resumes, progressive acquisition re-monitors only what the season-ahead rule calls for.
