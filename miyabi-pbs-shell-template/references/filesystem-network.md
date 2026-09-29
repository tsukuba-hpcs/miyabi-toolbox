# Storage, Network And Containers

## Storage Placement

- Put shared project data, large reusable caches and retained outputs under the
  project's `/work/<group>/...` path. Keep `/home` small; use `show_quota` for
  current byte and file-count limits rather than copying a quota value.
- Use assigned `$TMPDIR` or G's `$LOCALDIR` for job-local scratch when suitable.
  Inspect the paths in the allocation and create required subdirectories on
  each node. Copy needed outputs to shared storage before the job ends.
- `/tmp` is node-local. Use shared durable paths for retained logs and results;
  create log/publication parents before redirection or rank startup.
- Verify compute-node mounts before depending on `/common` or any path visible
  only from login. Filesystem visibility and executable architecture are
  separate checks.

For `No space left on device`, inspect both bytes and inode/file-count limits.
Use task-local caches for diagnostics; keep large temporary models outside small
validation-report packages. Changing home placement with `chhome` or moving
shell configuration needs authorization for that change.

## Network And Containers

Check the target allocation's current outbound connectivity before depending on
downloads or remote services; login connectivity does not establish compute-node
access. Use site-supported access for services instead of assuming public
inbound reachability.

Discover the container runtime through [module queries](module.md) and load it
in the job shell. Use the project's supported runtime and image; historical
Singularity examples do not determine the current module name. Bind required
paths, place large images/caches on suitable storage, and preserve outputs
outside job-local scratch.
