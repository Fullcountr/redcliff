# Releasing

The `redcliff` package is published to PyPI by [`.github/workflows/release.yml`](../.github/workflows/release.yml)
when a version tag is pushed.

## Once: set up trusted publishing

PyPI trusted publishing lets the workflow publish without a stored token.

1. Sign in at [pypi.org](https://pypi.org) and open **Your projects → Publishing**.
2. Under **Add a new pending publisher**, choose **GitHub** and enter: owner `Fullcountr`,
   repository `redcliff`, workflow `release.yml`, environment `pypi`. A pending publisher can
   create the project on its first upload.
3. In the GitHub repository, **Settings → Environments → New environment** named `pypi`.
   Optionally require a reviewer, so every release waits for an approval.

## Each release

1. Set the version in `core/src/redcliff/__init__.py` (`__version__ = "0.2.0"`) and commit it.
2. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.

The workflow checks that the tag matches the version, runs lint and the tests, builds the wheel and
sdist, installs the wheel in a clean environment and runs it, publishes to PyPI, then creates a
GitHub release with the files and generated notes.
