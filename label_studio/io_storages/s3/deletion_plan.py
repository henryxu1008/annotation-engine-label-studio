"""Non-authoritative planning for a version-pinned annotation deletion.

No function here sends a delete or establishes a cloud isolation boundary.
The observation is deliberately incapable of authorizing automatic execution.
"""

_VISIBILITY = {
    # Removing a noncurrent version leaves the observed current version or
    # delete marker unchanged, provided no writer changes the history.
    'noncurrent': 'CURRENT_VERSION_UNCHANGED_IF_HISTORY_STABLE',
    # Removing the current version could reveal any older object version;
    # pagination does not establish the next item across Versions/Markers.
    'current_with_other_object_versions': 'OLDER_OBJECT_MAY_BECOME_VISIBLE',
    'current_without_other_object_versions': 'NO_OLDER_OBJECT_OBSERVED',
}


def describe_version_deletion(history):
    """Describe one observed history without creating permission to act.

    The product requires deletion of the pinned version without making an
    older object visible. A later writer or a previously submitted request
    can invalidate any listing, including one that currently looks harmless.
    """
    if not isinstance(history, str) or history not in _VISIBILITY:
        raise ValueError('A recognized version history observation is required.')
    blockers = [
        'CLOUD_ISOLATION_AND_DRAIN_UNVERIFIED',
        'VERSION_HISTORY_NOT_A_SNAPSHOT',
        'OBJECT_LOCK_NOT_VERIFIED',
    ]
    if history == 'current_with_other_object_versions':
        blockers.append('OLDER_OBJECT_MAY_BECOME_VISIBLE')
    return {
        'semantics': 'DELETE_PINNED_VERSION_WITHOUT_REEXPOSING_OLDER_OBJECT',
        'history': history,
        'visibility': _VISIBILITY[history],
        'observation': 'NON_SNAPSHOT',
        'automatic_execution': 'BLOCKED',
        'blockers': blockers,
    }
