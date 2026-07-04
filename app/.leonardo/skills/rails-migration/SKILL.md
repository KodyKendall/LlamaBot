---
name: Rails Migration
description: Safely write and run a Rails database migration. Use when the user asks to add or change a column, table, or index, or mentions "migration", "schema", or "db:migrate".
---

# Rails migration

A focused playbook for changing the database schema in a LlamaPress Rails app.

## Steps
1. Generate the migration: `bin/rails g migration <DescriptiveName>` (e.g. `AddPublishedAtToPosts`).
2. Edit the generated file under `db/migrate/`. Prefer a reversible `change` method; use
   `up`/`down` only when the change can't be auto-reversed.
3. Run it: `bin/rails db:migrate`. Confirm `db/schema.rb` updated as expected.
4. If it fails, `bin/rails db:rollback`, fix the migration, and re-run.

## Rules & gotchas
- NEVER edit a migration that has already run in production — add a new migration instead.
- Adding a NOT NULL column to an existing table needs a default (or a backfill + a
  follow-up migration) or it will fail on non-empty tables.
- For large tables, adding an index can lock writes — call it out to the user before running.
- After migrating, restart is usually not required (Rails reloads in dev), but if models
  don't pick up the change, use the hard restart tool.

## Verify
- `bin/rails runner "puts <Model>.column_names"` to confirm the column exists.
- Re-read `db/schema.rb` to confirm the schema version bumped.
