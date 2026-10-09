# Releasing strands-sprites

Versions come from Git tags through `hatch-vcs`: `v0.1.0` builds version `0.1.0`.
Use a full Git checkout for development builds. Source distributions retain their
version metadata and can be built without Git. Untagged commits get development
versions; the publishing workflow rejects them.

The PyPI trusted publisher uses these settings:

| Setting | Value |
| --- | --- |
| Project name | `strands-sprites` |
| GitHub owner | `superfly` |
| Repository | `strands-sprites` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

This is configured in the PyPI project owner account, under
[Publishing](https://pypi.org/manage/account/publishing/). No API token is needed.
See the [PyPI trusted publishing guide](https://docs.pypi.org/trusted-publishers/).

To publish an approved release, create a
`vX.Y.Z` tag on a tested commit from `main` and publish its GitHub release. The
`publish.yml` workflow verifies main ancestry, runs checks, builds the wheel and
source archive, verifies that both versions match the tag, and uploads them through
PyPI trusted publishing. Pushing a tag alone does not publish a package.

The Strands catalog entry uses `integrationType: integration` and
`maintainedBy: partner`. Submit it after the package is available on PyPI and this
repository is public, and set `addedDate` to the submission date. The repository
should carry the `strands-agents` topic.

