# Business database backups

A business whose product keeps its own database elsewhere (for example a hosted
SQLite service) can have Qorqut pull a copy on a schedule. The program is
`backups.pull`; a business Routine runs it. Configuration never loads executable
code: the Routine names two business secrets and Qorqut does the rest.

## What it does

1. Reads the endpoint and bearer token from the business `.env` (the same scoped
   secret view and sandbox egress route `metrics.sync` uses). The endpoint must
   be HTTPS with no embedded credentials; redirects are not followed.
2. Streams the response to a private temporary file, at most 1 GiB and ten
   minutes, then requires it to be a gzipped SQLite database that passes
   `PRAGMA quick_check`. Anything else fails the job and keeps nothing.
3. Keeps the copy as
   `<businesses root>/.bazarlyk/business-backups/<business id>/<job time>-<job>.sqlite3.gz`
   (directories 0700, files 0600) and removes the oldest beyond `keep`.

That folder is outside Git, outside the business workspace that sandboxed
agents mount, and outside the nightly authored backup. Errors never contain the
endpoint, token or response body. The program is external with its own
recovery: a retried job whose copy is already kept does not download again.

## Setting one up

The product serves `GET <endpoint>` with `Authorization: Bearer <token>`,
returning `application/gzip`. Then, for the business:

1. Set two secrets with `environment_set`, for example `APP_BACKUP_URL` and
   `APP_BACKUP_TOKEN`.
2. Create a Routine: program `backups.pull`, arguments
   `{"endpoint_env": "APP_BACKUP_URL", "token_env": "APP_BACKUP_TOKEN", "keep": 60}`
   (`keep` 1–365, default 30), a cron schedule and timezone. Create it paused,
   run it once with `routines_run`, check the job and the kept file, then resume.

To restore, `gunzip` a copy and put it back where the product expects its
database, with the product stopped. Restoring is a manual, reviewed action.

## Rollout

Like any new program, `backups.pull` needs its system worker principal and
memberships, registered with the existing `BackgroundAuthority.register_workers`
maintenance path before first use. Until then business creation also refuses,
because it requires a worker for every business-scope program. Registration
enables no Routine.
