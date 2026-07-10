# Git Synchronization Between Local And Miyabi

Use this reference only when tracked source must move between a local checkout
and Miyabi for static or runtime validation.

## Preconditions

Inspect rather than assuming a clean worktree:

```bash
git status --short --branch
git remote -v
git rev-parse --show-toplevel
git rev-parse --short HEAD
```

Preserve unrelated user changes. Reuse an existing task branch when it is
clearly in scope; otherwise create a `codex/<short-task-name>` branch. Do not
switch branches if doing so would overwrite or hide user work.

## Synchronize Tracked Source

On the local checkout:

```bash
git switch -c codex/<short-task-name>
git add <changed-files>
git commit -m "<focused message>"
git push -u origin codex/<short-task-name>
git rev-parse --short HEAD
```

On the Miyabi login node:

```bash
git fetch origin
git switch codex/<short-task-name>
git pull --ff-only
git rev-parse --short HEAD
```

Verify that the branch and commit match before runtime validation. Send fixes
back as ordinary follow-up commits on the same branch.

## Authority Boundaries

- Do not merge to `main` unless the user explicitly authorizes that merge.
- Do not use `git reset --soft origin/main`, interactive rebase, force-push, or
  other history rewriting as automatic cleanup.
- Prefer a clean sequence of focused commits or a squash-at-merge workflow.
- If the user explicitly requests history cleanup, inspect the branch base and
  coordinate before rewriting shared history; use `--force-with-lease` only
  after that approval.

## Direct Copy Exception

Use `rsync` only for logs, scratch artifacts, or intentionally untracked files:

```bash
rsync -azn <local-path> miyabi-g:<remote-path>
```

Run the dry run first. If GitHub is unavailable and tracked source must be
copied directly, preserve the destination, reconcile the result back into Git
on both sides immediately, and report that the normal branch workflow was
bypassed.

## Handoff

Report the branch, exact commit, remote checkout path, validation performed,
and whether the branch is only ready for merge. Keep the commit graph visible
enough that the user can reproduce the Miyabi state.
