# Contributing

Use a focused change with a concise explanation and the checks that demonstrate
its behavior. Include documentation when a user-visible operation changes.
Keep credentials, personal records, local configuration and machine paths out of
code, examples, screenshots and test fixtures. Use synthetic test data.

## Branches

Create work branches from `dev`. Open pull requests against `dev`. A pull
request into `dev` needs the maintainer's approval and passing checks.

`main` holds released code and takes pull requests only from `dev`. They need
the maintainer's approval. Neither branch accepts direct pushes or force-pushes.

## Rights and license

Only submit work you have the right to contribute. Preserve existing third-party
copyrights, licenses and provenance. Identify copied or adapted material and its
source in the pull request; do not relabel it as first-party code.

### Contributor license agreement

Qorqbot is distributed under the PolyForm Noncommercial License and under
commercial licenses sold by 49 Agents LLC. By submitting a contribution (code,
documentation, assets or other material) to a first-party part of this
repository, you agree that:

- You keep the copyright in your contribution.
- You grant 49 Agents LLC a perpetual, worldwide, non-exclusive, royalty-free,
  irrevocable license to use, copy, modify, sublicense and distribute your
  contribution under the project's current or future licenses, including
  49 Agents LLC's commercial licenses.
- You grant 49 Agents LLC and recipients of the project a patent license for
  your contribution on the same terms.
- You have the right to grant these licenses. If your employer or a client has
  rights in the work, you have its permission or it has waived those rights.
- Your contribution is provided as is, without warranty.

Contributions to a separately licensed component, such as the WhatsApp native
helper or vendored third-party files, follow that component's license instead.

State in your pull request that you agree to these terms. The maintainer
confirms this before merging an outside contribution.

Use the supported versions and standard tools documented by the project. Changes
to optional integrations must keep core usable when the integration is absent.
