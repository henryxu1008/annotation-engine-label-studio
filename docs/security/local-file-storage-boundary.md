# Local file storage boundary

Local file storage access rejects configured private roots and uses platform-specific
handle-relative operations to avoid following links or trusting a path after it has
been authorized.

On Linux, this protects against filesystem changes made by a different OS identity
when directory permissions prevent that identity from moving or replacing the
storage root. A process running as the Label Studio service UID is part of the
trusted computing base: Linux does not isolate processes that share a UID, and a
same-UID process can access the service's files and mutate directories that the UID
can write. This is not a boundary the file-open implementation can enforce.

Deploy Label Studio under a dedicated, non-root service account. Do not run
untrusted local jobs, plugins, or helper processes as that account. If same-UID
processes must be treated as hostile, isolate them with a separate UID and an
appropriate container or mount boundary instead of relying on this application
guard.
