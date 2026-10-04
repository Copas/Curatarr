# 003: Persist external actions

The webhook only writes an event. The worker turns observations into persistent, uniquely keyed actions and executes them after commit. Failed acquisition may retry. An uncertain deletion response enters an unknown state and requires reconciliation rather than automatic retry.

## Versioned policy format (spec section 92)

Policy values are stored as JSON: the global setting, library policies, and title overrides. The format version is recorded in `app_settings.policy_schema_version` (currently 1, by migration `c4e8a1f07d36`), and the code declares the version it understands in `curatarr.schema.POLICY_SCHEMA_VERSION`. A change in meaning ships as an Alembic data migration that rewrites stored values and raises both. Code that finds a newer stored version refuses to run, with a clear 503, rather than reinterpreting settings. Unknown fields are always rejected, never ignored.
