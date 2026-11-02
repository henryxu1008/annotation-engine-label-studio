"""Command entrypoint for enterprise UI CI. No product server imports unless explicitly running a shard."""
import argparse
import os
from pathlib import Path

from ui_common import artifact_name, context, file_hash, read_json, require
from ui_inventory import SHARDS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['matrix', 'dependencies', 'prepare', 'unpack', 'browser', 'aggregate'])
    parser.add_argument('--local', action='store_true')
    parser.add_argument('--shard', choices=list(SHARDS))
    parser.add_argument('--out', type=Path)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--build-artifact-id', type=int)
    args = parser.parse_args()
    if args.command == 'matrix':
        import json

        print(json.dumps({'shard': list(SHARDS)}))
        return
    ctx = context(args.local)
    if args.command == 'dependencies':
        from ui_dependencies import configure

        configure(ctx)
        return
    if args.command == 'unpack':
        from ui_artifacts import unpack_build

        require(args.package is not None, 'Build package required')
        manifest = unpack_build(ctx, args.package)
        print('UI_BUILD_VERIFIED', manifest['archive_sha256'])
        return
    require(args.out is not None, 'Unique evidence output required')
    if args.command == 'aggregate':
        require(
            not args.local, 'Offline aggregate acceptance uses explicit fixture tests, never a real required check'
        )
        from ui_aggregate import aggregate

        aggregate(ctx, args.out)
        return
    from ui_run import browser, prepare

    if args.command == 'prepare':
        success = prepare(ctx, args.out)
        logical = 'prepare'
    else:
        require(
            args.shard is not None and args.package is not None and args.build_artifact_id is not None,
            'Shard needs an explicit verified build identity',
        )
        from ui_artifacts import DIST, unpack_build

        # unpack command is a separate workflow step; verify the installed dist again.
        from ui_common import regular_tree, same_source

        manifest = read_json(args.package / 'manifest.json')
        same_source(ctx, manifest['context'])
        require(regular_tree(DIST) == manifest['files'], 'Installed build changed before browser')
        build = {
            'artifact_id': args.build_artifact_id,
            'producer_attempt': manifest['context']['attempt'],
            'manifest_sha256': file_hash(args.package / 'manifest.json'),
            'archive_sha256': manifest['archive_sha256'],
        }
        logical = args.shard
        success = browser(ctx, args.shard, args.out, build, manifest)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            output.write('artifact-name=' + artifact_name(ctx, 'evidence', logical) + '\n')
            if logical == 'prepare':
                output.write('build-name=' + artifact_name(ctx, 'build', logical) + '\n')
    raise SystemExit(0 if success else 1)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Do not dump request headers, fixture contents or raw test output into CI artifacts.
        print('UI_EVIDENCE_FAILED', flush=True)
        raise
