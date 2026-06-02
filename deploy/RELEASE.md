# ASTRAL Public Release Checklist

Use this checklist before publishing a new sanitized version of ASTRAL to the public repository.

## Pre-release scan

Run the following commands from the repository root to ensure no tenant-specific data remains:

```bash
# Search for the original tenant identifiers
grep -ri "cqre" --include="*.{yml,yaml,py,md,json,sh,ps1}" . | grep -v node_modules | grep -v __pycache__ | grep -v .git
grep -ri "kracmar" --include="*.{yml,yaml,py,md,json,sh,ps1}" . | grep -v node_modules | grep -v __pycache__ | grep -v .git
grep -ri "sc_intunebackup" --include="*.{yml,yaml,py,md,json,sh,ps1}" . | grep -v node_modules | grep -v __pycache__ | grep -v .git

# Search for the original tenant ID (replace with your actual tenant ID)
grep -ri "0ec9f34c-17c8-4541-b084-7d64ecdcc997" --include="*.{yml,yaml,py,md,json,sh,ps1}" . | grep -v node_modules | grep -v __pycache__ | grep -v .git
```

Expected result: **zero matches** outside of this release checklist.

## File verification

- [ ] `azure-pipelines.yml` contains no hardcoded tenant domain, email, or service connection name.
- [ ] `azure-pipelines-restore.yml` contains no hardcoded tenant domain, email, or service connection name.
- [ ] `azure-pipelines-review-sync.yml` contains no hardcoded tenant-specific values.
- [ ] `scripts/common.py` uses a generic fallback name (not `CQRE_Intune_Backupper`).
- [ ] `infra/change-probe/` contains no tenant-specific IDs, secrets, or connection strings.
- [ ] `infra/change-probe/local.settings.json` is excluded (only `.example` should exist).
- [ ] `tenant-state/` contains only placeholder files (`.gitkeep`, `README.md`).
- [ ] `prod-as-built.md` has been deleted.
- [ ] All markdown documentation uses generic examples (`contoso.onmicrosoft.com`, `astral-backup@contoso.com`, `sc-astral-backup`).

## Test verification

- [ ] Unit tests pass: `python3 -m unittest discover -s tests -v`

## Publication steps

1. Ensure you are on a clean branch (e.g. `publish/v1.x`).
2. Run the pre-release scan above.
3. Commit any last-minute fixes.
4. Tag the release: `git tag -a v1.0.0 -m "ASTRAL v1.0.0"`
5. Push the tag.
6. Publish to the public repository (fresh clone or specific branch push).

## Note on Git history

If the original repository contained live tenant exports in its history, consider publishing from a **squashed or freshly initialized repository** rather than pushing the full private history. The public template does not benefit from historical tenant data, and a clean history avoids accidental exposure of old exports.
