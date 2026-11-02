"""Package only static dist; verify an untrusted archive before installing it."""
import hashlib
import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path

from ui_common import (
    BUILD_ARCHIVE_LIMIT,
    BUILD_COUNT,
    BUILD_FILE_LIMIT,
    BUILD_LIMIT,
    RECEIPT_LIMIT,
    REPORT_LIMIT,
    REPORT_UNION_LIMIT,
    ROOT,
    canonical,
    file_hash,
    integer,
    loads,
    public_evidence,
    read_json,
    regular_tree,
    relative_path,
    require,
    same_source,
    sha256,
    write_json,
)

DIST = ROOT / 'web/dist/apps/labelstudio'


def static_name(name):
    relative_path(name)
    require(
        not any(v.startswith('.') or v.endswith(('.env', '.pem', '.key')) for v in name.split('/')),
        'Nonstatic/private build member',
    )


def pack_build(ctx, output, source=DIST):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rows = regular_tree(source)
    for row in rows:
        static_name(row['path'])
    with tarfile.open(output / 'frontend.tar', 'w', format=tarfile.PAX_FORMAT) as archive:
        for row in rows:
            archive.add(Path(source) / row['path'], arcname=row['path'], recursive=False)
    require((output / 'frontend.tar').stat().st_size <= BUILD_ARCHIVE_LIMIT, 'Build archive budget exceeded')
    manifest = {
        'schema': 'ui-build/v1',
        'context': ctx,
        'files': rows,
        'archive_sha256': file_hash(output / 'frontend.tar', BUILD_ARCHIVE_LIMIT),
    }
    write_json(output / 'manifest.json', manifest)
    return manifest


def unpack_build(ctx, package, destination=DIST):
    package = Path(package)
    destination = Path(destination)
    manifest = read_json(package / 'manifest.json')
    require(set(manifest) == {'schema', 'context', 'files', 'archive_sha256'}, 'Build manifest fields invalid')
    require(manifest.get('schema') == 'ui-build/v1', 'Build schema invalid')
    same_source(ctx, manifest['context'])
    rows = manifest['files']
    require(isinstance(rows, list) and 0 < len(rows) <= BUILD_COUNT, 'Build member count invalid')
    expected = {}
    total = 0
    folded = set()
    for row in rows:
        require(set(row) == {'path', 'size', 'sha256'}, 'Build member shape invalid')
        static_name(row['path'])
        require(row['path'].casefold() not in folded, 'Duplicate build member')
        folded.add(row['path'].casefold())
        require(integer(row['size'], 0, BUILD_FILE_LIMIT), 'Build member size invalid')
        require(sha256(row['sha256']), 'Build member hash invalid')
        total += row['size']
        require(total <= BUILD_LIMIT, 'Build union budget exceeded')
        expected[row['path']] = row
    archive_path = package / 'frontend.tar'
    require(
        file_hash(archive_path, BUILD_ARCHIVE_LIMIT) == manifest['archive_sha256'],
        'Build archive digest mismatch',
    )
    require(not destination.exists() and not destination.is_symlink(), 'Never overwrite a consumer dist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Every existing parent must be literal, not a link into another checkout.
    require(destination.parent.resolve() == destination.parent.absolute(), 'Symlinked destination parent')
    staging = Path(tempfile.mkdtemp(prefix='.ui-verified-', dir=destination.parent))
    try:
        with tarfile.open(archive_path, 'r:') as archive:
            seen = set()
            for entry in archive:
                relative_path(entry.name)
                require(entry.isfile() and not entry.issym() and not entry.islnk(), 'Only regular build files allowed')
                require(entry.name in expected and entry.name not in seen, 'Extra/duplicate build archive member')
                seen.add(entry.name)
                row = expected[entry.name]
                require(entry.size == row['size'], 'Archive member size mismatch')
                target = staging / entry.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(entry) as inp, target.open('xb') as out:
                    used = 0
                    for block in iter(lambda: inp.read(1024 * 1024), b''):
                        used += len(block)
                        require(used <= row['size'], 'Archive expanded beyond budget')
                        out.write(block)
                require(used == row['size'] and file_hash(target) == row['sha256'], 'Build file digest mismatch')
            require(seen == set(expected), 'Build archive missing files')
        require(regular_tree(staging) == rows, 'Build manifest inventory mismatch')
        staging.rename(destination)
    finally:
        if staging.exists():
            # Exact directory created above, never an input-computed recursive target.
            require(
                staging.resolve().parent == destination.parent.resolve()
                and staging.name.startswith('.ui-verified-')
                and not staging.is_symlink(),
                'Unsafe staging cleanup target',
            )
            shutil.rmtree(staging)
    return manifest


def evidence_files(folder, receipt):
    """Validate the exact export list. Raw runner output/fixtures never enter an artifact."""
    folder = Path(folder)
    require(receipt.get('schema') == 'ui-shard-receipt/v1', 'Receipt schema invalid')
    calls = receipt.get('calls')
    require(isinstance(calls, list) and len(calls) <= 128, 'Receipt call budget exceeded')
    files = [
        {
            'path': 'receipt.json',
            'size': (folder / 'receipt.json').stat().st_size,
            'sha256': file_hash(folder / 'receipt.json', RECEIPT_LIMIT),
        }
    ]
    total = 0
    seen = set()
    for call in calls:
        if call.get('status') != 'success' and call.get('report') is None:
            continue
        r = call['report']
        require(set(r) == {'path', 'size', 'sha256'}, 'Report reference invalid')
        relative_path(r['path'])
        require(
            r['path'].startswith('reports/') and r['path'].endswith('.json') and r['path'] not in seen,
            'Duplicate or private report',
        )
        seen.add(r['path'])
        require(integer(r['size'], 1, REPORT_LIMIT), 'Report size invalid')
        total += r['size']
        require(total <= REPORT_UNION_LIMIT, 'Report union budget exceeded')
        require(
            (folder / r['path']).stat().st_size == r['size']
            and file_hash(folder / r['path'], REPORT_LIMIT) == r['sha256'],
            'Report digest mismatch',
        )
        read_json(folder / r['path'])
        files.append(r)
    return files


def prepare_export(folder, receipt):
    """Producer allowlist is enforced before upload, independently of receiver checks."""
    folder = Path(folder)
    public_evidence(receipt)
    on_disk = read_json(folder / 'receipt.json', RECEIPT_LIMIT)
    require(canonical(on_disk) == canonical(receipt), 'Receipt changed before export')
    files = evidence_files(folder, receipt)
    actual = regular_tree(folder, count=129, total=REPORT_UNION_LIMIT + RECEIPT_LIMIT, single=REPORT_LIMIT)
    require({r['path'] for r in actual} == {r['path'] for r in files}, 'Extra/private producer evidence file')
    export = folder.parent / (folder.name + '-export')
    require(not export.exists(), 'Never overwrite exported evidence')
    verified = []
    for item in files:
        src = folder / item['path']
        require(not src.is_symlink(), 'Producer evidence symlink')
        cap = RECEIPT_LIMIT if item['path'] == 'receipt.json' else REPORT_LIMIT
        with src.open('rb') as reader:
            require(stat.S_ISREG(os.fstat(reader.fileno()).st_mode), 'Nonregular producer evidence')
            raw = reader.read(cap + 1)
        require(
            len(raw) == item['size'] and len(raw) <= cap and hashlib.sha256(raw).hexdigest() == item['sha256'],
            'Producer evidence changed before export',
        )
        public_evidence(loads(raw, cap))
        verified.append((item['path'], raw))
    # Nothing is publishable until every member passed the producer privacy gate.
    export.mkdir()
    for name, raw in verified:
        target = export / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as writer:
            writer.write(raw)
    require(
        regular_tree(export, count=129, total=REPORT_UNION_LIMIT + RECEIPT_LIMIT, single=REPORT_LIMIT) == actual,
        'Export staging mismatch',
    )
    return export


def unpack_evidence_zip(data, destination):
    """Aggregate downloads are isolated; no ZipFile.extract()/extractall()."""
    import io

    require(len(data) <= REPORT_UNION_LIMIT + RECEIPT_LIMIT + 1024**2, 'Evidence download budget exceeded')
    destination = Path(destination)
    require(not destination.exists(), 'Never overwrite evidence download')
    destination.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        require(0 < len(entries) <= 129, 'Evidence archive file-count exceeded')
        total = 0
        seen = set()
        for e in entries:
            relative_path(e.filename)
            require(
                not e.is_dir() and (e.external_attr >> 16) & 0o170000 not in (0o120000, 0o060000, 0o020000),
                'Nonregular evidence member',
            )
            require(e.filename.casefold() not in seen, 'Duplicate evidence path')
            seen.add(e.filename.casefold())
            require(
                e.filename == 'receipt.json' or (e.filename.startswith('reports/') and e.filename.endswith('.json')),
                'Unexpected/private evidence member',
            )
            cap = RECEIPT_LIMIT if e.filename == 'receipt.json' else REPORT_LIMIT
            require(0 < e.file_size <= cap, 'Evidence archive member budget')
            total += e.file_size
            require(total <= REPORT_UNION_LIMIT + RECEIPT_LIMIT, 'Evidence archive union budget')
            target = destination / e.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(e) as inp, target.open('xb') as out:
                raw = inp.read(cap + 1)
                require(len(raw) == e.file_size and len(raw) <= cap, 'Evidence decompression mismatch')
                out.write(raw)
    receipt = read_json(destination / 'receipt.json', RECEIPT_LIMIT)
    allowed = {r['path'] for r in evidence_files(destination, receipt)}
    actual = {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()}
    require(actual == allowed, 'Unexpected/omitted evidence files')
    return receipt
