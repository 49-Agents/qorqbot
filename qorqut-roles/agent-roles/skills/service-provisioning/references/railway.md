# Railway project

Work in your own browser (`browser_turn`) in the logged-in, chosen Railway
workspace.

1. Reconcile first. Read the Task's recorded Railway evidence and the
   `environment_list` flags for `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID`
   and `RAILWAY_TOKEN`. Look for an existing Railway project for this Qorqut
   project and check its IDs. Reuse a matching one; otherwise create one Railway
   project for it.
2. Use its `production` environment, creating it only if the Railway project lacks one.
3. Nothing else is implicit: no service deployment, repository attachment,
   database purchase, team or account-level key.
4. In the Railway project settings, create a project token for the production
   environment. It is shown once: save it with `environment_set` as
   `RAILWAY_TOKEN` before closing the dialog or navigating away. Then save
   `RAILWAY_PROJECT_ID` and `RAILWAY_ENVIRONMENT_ID` the same way.
5. Verify in the UI that the Railway project, its production environment and the
   token's environment scope are the ones saved. Railway project tokens are
   scoped to one environment of one Railway project.
6. Record on the Task: the workspace ID (nonsecret metadata), Railway project ID,
   environment ID and name (`production`), the time the token scope was verified
   and the settings receipt `{keys, shared_hash?, private_hash?}` from the
   `environment_set` results, covering all three keys.

If a token was created but the save is uncertain, check the `environment_set`
receipt, the `environment_list` flag and the Railway project's token list before
creating another. A lost value becomes an explicit recovery step for the owner; do not create
or revoke tokens blindly.
