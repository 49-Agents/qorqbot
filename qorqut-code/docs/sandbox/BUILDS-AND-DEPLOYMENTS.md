# Business builds and deployments

Ordinary Python, Node and C/C++ builds run in the business workstation. For
Docker-based projects, use a workflow in the business's own repository on a
GitHub-hosted runner. The runner builds away from the Qorqut host; the workstation
does not receive a Docker socket, privileged container or host administrator
credential. This uses ordinary Git and `gh`, without a new Qorqut build module.

No agent receives GitHub provider credentials. A workflow uses its
own short-lived repository `GITHUB_TOKEN` with explicit permissions. Build images
by immutable base digest, keep credentials out of the build context, and choose
timeouts and artifact retention appropriate to the repository. GitHub documents
the [hosted runner boundary](https://docs.github.com/en/actions/concepts/runners/github-hosted-runners)
and [workflow permissions and limits](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).

## Disposable build workflow

A remote-build workflow for a private repository should pin its base image and checkout
action, avoid persisted checkout credentials, grant the job only repository-content read
access, and build and run the application in a container with no network, a read-only root,
no capabilities and bounded memory/CPU/processes. The job verifies a health response and
removes its own container afterward; it creates no persistent application or image registry
artifact. Credentials for pushing workflow files stay on the trusted host and are never
given to the workstation.

## Deployment

A deployment provider/project is chosen per project. Its scoped credential must
grant only that project's resources. The application should run at that target
independently of the business workstation; verify a deployment, workstation
replacement without application interruption, and rollback of the application
revision. A passing remote build or a private development preview does not
establish those properties. No new management interface is required.
