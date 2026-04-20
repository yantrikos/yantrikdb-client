# PyPI Trusted Publisher setup — yantrikdb-client

One-time configuration so `.github/workflows/publish.yml` can publish
the SDK to PyPI via OIDC. No long-lived tokens stored.

## One-time setup

### 1. PyPI: replace the trusted publisher

The project was originally published from `yantrikos/yantrikdb-server`
(sdk subdirectory). Now that `yantrikos/yantrikdb-client` is the
canonical home, the trusted publisher config must be updated.

1. Go to https://pypi.org/manage/project/yantrikdb-client/settings/publishing/
2. **Remove** the existing publisher pointing at `yantrikos/yantrikdb-server`
   with workflow `publish-sdk.yml`
3. **Add a new publisher** with:
   - **PyPI project name**: `yantrikdb-client`
   - **Owner**: `yantrikos`
   - **Repository name**: `yantrikdb-client`
   - **Workflow name**: `publish.yml`
   - **Environment name**: `pypi`

### 2. TestPyPI (optional, recommended for dry runs)

1. Go to https://test.pypi.org/manage/project/yantrikdb-client/settings/publishing/
2. Update similarly with Environment name `testpypi`

### 3. GitHub: create the `pypi` environment

1. https://github.com/yantrikos/yantrikdb-client/settings/environments → "New environment"
2. Name: `pypi`
3. Recommended protections:
   - **Required reviewers**: add yourself — every real publish needs a human click
   - **Deployment branches and tags**: restrict to tags matching `v*`
4. Save.

Repeat with name `testpypi` for the TestPyPI dry-run path (required reviewer optional there).

## How to release

```bash
# 1. Bump version in pyproject.toml
# 2. Commit
git commit -am "v0.3.0 release"

# 3. Tag (no 'sdk-' prefix needed — this repo IS the SDK)
git tag -a v0.3.0 -m "release notes..."

# 4. Push tag
git push origin v0.3.0
```

The workflow fires on tag push. If the `pypi` environment has
required reviewers, approve the job in the Actions tab.

## Dry run via TestPyPI

Actions tab → "Publish to PyPI" → Run workflow → select `testpypi` as
target. TestPyPI receives the build without touching the real index.
