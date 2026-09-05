# Falguna Bootstrap v0.1

The earliest safe self-building control plane. It runs one bounded repository task at a time in an isolated Git worktree, with deny-by-default permissions, checkpoints, verification, independent review, evidence, cost records, and a human-only proposed-merge gate.

It is not the Company OS and never merges or deploys automatically.

## Local commands

```sh
python3 -m unittest discover -s tests -v
python3 -m falguna init
python3 -m falguna create-mission --title "Example" --requirement "Bounded change"
python3 -m falguna status
```

SQLite is the durable local development store because PostgreSQL is not installed on the current Intel Mac. `schema/postgres.sql` is the canonical promotion schema and storage access stays behind `StateStore`.

