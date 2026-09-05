# Filesystem, Network, And Container Tips

Use this reference for Miyabi storage placement, job-local scratch, quota
diagnosis, compute-node network access, and container setup.

## Choose Storage

- Use `/work/<group>/...` for shared durable project data and outputs.
- Keep `/home` light. The Miyabi User's Guide v1.8 documents a 50 GB user
  quota; inspect current usage with `show_quota` instead of assuming remaining
  capacity.
- On compute nodes, use the assigned `$TMPDIR` and, on Miyabi-G, `$LOCALDIR`
  for job-local scratch when local I/O is useful.
- Copy required outputs from job-local scratch to durable storage before the
  allocation ends.
- A `/tmp` path on a compute node is not the same storage as `/tmp` on login.
  Put retained validation logs on shared durable storage; create their parents
  before shell redirections/`tee` start. For distributed publication, create
  required shared parents before launching ranks.
- Do not assume `/common` is visible from compute nodes; the v1.8 guide marks
  it as login-node-only.

For `No space left on device`, inspect both byte quota and directory/file-count
limits before changing the workload or retrying the operation.
Keep temporary model trees and compiler caches outside small checked report
packages; retain the required compact results and explicit artifact locations.
See [failure-lessons.md](failure-lessons.md) for the observed path failures.

Do not run `chhome`, move dotfiles, or alter shell startup files without an
explicit user request. When editing shell initialization, do not globally put
custom paths ahead of Miyabi system paths unless that precedence is intended.

## Network Access

The v1.8 guide documents outbound HTTP/HTTPS access from Miyabi-G compute nodes
through NAT and no inbound access to compute nodes. Inspect current site rules
and connectivity from the relevant allocation before depending on a remote
service or download.

Do not expose a compute-node service or copy credentials into scripts, logs,
or shared storage. Use existing approved credential and secret mechanisms.

## Containers

Discover the current container module name and version with `module` or
`show_module`, then load it inside the job shell. Historical guide examples use
Singularity, but the live module catalog is authoritative.

Keep container images and large caches on storage appropriate to their size
and reuse. Bind only the directories required by the workload, and preserve
outputs outside ephemeral job-local storage before the job ends.
