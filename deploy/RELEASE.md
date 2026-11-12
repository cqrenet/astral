# ASTRAL Release Checklist

This repository (`cqrenet/astral`) is the public template. Releases are tagged on `main` and published
with the GitHub CLI. Customers update through `deploy/update-from-upstream.yml`.

## Pre-release scan

Scan tracked files only, so local scratch folders (`panes/`, `local-only/`) do not matter here:

```bash
git grep -nIi -E "kracmar|sc_intunebackup|CQRE_Intune" -- . ':!deploy/RELEASE.md'
git grep -nIi -E "<your-tenant-id>|<your-tenant-domain>" -- . ':!deploy/RELEASE.md'
git grep -nIi "cqre" -- . ':!deploy/RELEASE.md' | grep -vi cqrenet
```

Expected result: no tenant identifiers. Remaining `cqre` hits must be intentional (company name, release
tooling defaults, test fixtures).

## File verification

- [ ] `git ls-files tenant-state` lists only `README.md` and `.gitkeep` placeholders.
- [ ] `git ls-files | grep -E "PRICING|SUPPORT|security-review|panes/|local-only/|local.settings.json"` is empty.
- [ ] No hardcoded tenant domain, email or service connection name in `azure-pipelines*.yml`, `.github/` or `.gitea/`.
- [ ] `infra/change-probe/` has no tenant IDs, secrets or connection strings (only `local.settings.json.example`).
- [ ] Documentation uses generic examples (`contoso.onmicrosoft.com`, `astral-backup@contoso.com`, `sc-astral-backup`).

## Test verification

```bash
pip install pyyaml          # without it the pipeline-policy tests are skipped
python3 -m unittest discover -s tests -v
```

All tests must pass with none skipped.

## Publication steps

1. Make sure `main` contains everything to ship and the working tree is clean.
2. Run the pre-release scan and the tests above.
3. Tag with the release notes in the annotation (they become the GitHub release body):
   `git tag -a v1.x.y -F release-notes.txt`
4. Push `main` and the tag. Never force-push `main`; if `origin/main` has diverged, join the histories with
   `git merge -s ours origin/main` first.
5. Create the release: `./deploy/create-github-release.sh v1.x.y [previous-tag]`

Only `main` and release tags should be pushed. Delete any local backup branches first.

## Git history

Do not publish history that contains live tenant exports or dev-only material (pricing, support terms, security
review documents). Keep those in the gitignored `local-only/` folder.
