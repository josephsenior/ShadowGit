# Security policy

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could cause
workspace data loss, path traversal, or writes outside the selected workspace.
Use GitHub's private vulnerability reporting for the ShadowGit repository.

Include the affected version, operating system, reproduction steps, and whether
the workspace contains symlinks or junctions. Maintainers will acknowledge a
report as soon as practical and coordinate disclosure after a fix is available.

## Safety boundary

ShadowGit is a recovery tool, not a sandbox. It validates its own restore paths
and avoids following workspace symlinks, but it does not make untrusted code or
concurrent external filesystem mutation safe to execute.
