# Writing a migration

Atriolyric has no Alembic. `server/app/migrations.py` holds an ordered list of small steps that bring an older database up to what the models expect.

## How it runs

`run_migrations()` runs at startup (`main.py`):

1. If the database's `schema_version` is higher than the number of steps this release knows, it refuses to start (see below).
2. If the database already has tables and steps are pending, it copies the database to `data/backups/` first (see below).
3. `Base.metadata.create_all` creates tables that do not exist. It never alters an existing table.
4. Each step from the stored version onward runs in order. The new version is written to `system_settings['schema_version']` after each one, so an interrupted upgrade resumes where it stopped.
5. Data backfills run (see below).

A database from before versioning has no version row and counts as 0, so every step runs on it. A fresh database runs every step too, against tables `create_all` just made, so each step must be a no-op there.

## Schema steps

A schema step is a function named `ensure_*` (or a verb that says what it does, such as `drop_ratings_table`) in `migrations.py`.

Rules:

- **Idempotent.** Check before changing: read `PRAGMA table_info(<table>)` and only `ALTER TABLE ... ADD COLUMN` if the column is missing; use `CREATE INDEX IF NOT EXISTS`. Guard on the table existing (`if existing and ...`), because the table may not exist yet on an old database.
- **Plain SQL, never the ORM.** Use `engine.connect()` and `conn.exec_driver_sql(...)`. An ORM query selects every column the current models declare, including ones a later step has not added yet, and fails on an old database.
- **Commit** the connection at the end of the step.
- Keep it small. One step per change.

Never add to a step that has already been released, even one that touches the same table: every existing database has run it and will not run it again, so the change reaches fresh databases only. A new column always gets a new step.

Then append the function to the end of `MIGRATIONS`. Never reorder, rename or remove an entry: databases already past it would skip whatever moved into its slot. The version number is the position in the tuple.

Also change `server/app/models.py` to match. A fresh database gets the column from the model; an old one gets it from the step.

## Data backfills

When existing rows need filling in with something that needs the ORM or application code (relinking artists, rebuilding albums), use a data backfill instead of a schema step:

1. Write `backfill_<name>()` using `SessionLocal()` and the models.
2. Append `("backfill_<name>", backfill_<name>)` to `DATA_BACKFILLS`.

Backfills run after every schema step, so all model columns exist however old the database was. Each runs once, recorded under its own key in `system_settings`. Append only; never rename a key, or the backfill runs again.

## Tests and the frozen schema

`server/tests/test_migrations.py` has a parity test. `server/tests/fixtures/schema_78621d2.sql` is the schema of a database created at an old commit, the oldest one the current steps still upgrade cleanly. The test loads it, runs the migrations and compares the result with what today's models declare (tables, columns, indexes). A second test checks a fresh database against the models too. So a model change with no matching step fails the test.

When you add a step:

- Run the migration tests and make the parity test pass.
- Add a focused test for the step if it moves data (build an old-shaped table, run the step, check the rows).
- Do not edit the frozen fixture. It is a record of the past; if an old step ever stops working on it, that is the problem to fix.

## Backup before pending steps

Before any step runs on an existing database, `backup_database(version)` copies it to `<data dir>/backups/atriolyric-v<version>-<UTC timestamp>.db` using SQLite's online backup, which includes the WAL contents. `<version>` is the schema version being upgraded from. The newest three copies are kept (`BACKUPS_KEPT`). No copy is made for a fresh database, or when no schema step is pending. A backfill alone does not trigger one.

## Refusing a newer schema

If the stored version is higher than `len(MIGRATIONS)`, the database was upgraded by a newer release. `run_migrations()` raises `NewerSchemaError` and the server does not start, rather than run old code against columns it does not know. Setting `ATRIOLYRIC_ALLOW_NEWER_SCHEMA=true` (also `1` or `yes`) starts it anyway, creates only missing tables and changes nothing else. See [operations.md](operations.md).
