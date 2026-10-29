"""Audit only explicitly selected public files, without exporting secret values."""
import argparse
import json
import re
import sqlite3
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_NAMES = {'access.json', 'assistant.db', 'cookies', 'cookies-journal', 'login data',
                 'local state', 'preferences', 'secure preferences', '.env'}
PRIVATE_DIRS = {'data-api', '.venv', '__pycache__', '.git', '.codex', '.agents'}


def check_name(name, source=False):
    parts = Path(name.replace('\\', '/')).parts
    lower = [part.lower() for part in parts]
    if any(part in PRIVATE_NAMES or part in PRIVATE_DIRS for part in lower):
        raise ValueError('Private runtime path found: '+name)
    if 'node_modules' in lower and (source or 'licenses' not in lower):
        raise ValueError('Dependency working directory in public files: '+name)
    if source and any(part in {'data', 'build', 'release', 'dist'} for part in lower):
        raise ValueError('Runtime/build directory in source: '+name)
    public_ca_bundle = not source and lower[-3:] == ['_internal', 'certifi', 'cacert.pem']
    if any(part.endswith(('.db', '.sqlite', '.sqlite3', '.log', '.key')) or
           (part.endswith('.pem') and not public_ca_bundle) for part in lower):
        raise ValueError('Private runtime file found: '+name)


def local_needles():
    # Values are never returned in reports or printed. This only supplements
    # the structural allowlist; pristine browser binaries contain no profile.
    values = set()
    for folder in ('data', 'data-api'):
        access = ROOT/folder/'access.json'
        if access.exists():
            value = json.loads(access.read_text(encoding='utf-8')).get('code', '')
            if len(value) >= 6: values.add(value)
        dbpath = ROOT/folder/'assistant.db'
        if dbpath.exists():
            with sqlite3.connect(dbpath.resolve().as_uri()+'?mode=ro', uri=True) as db:
                columns = {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
                candidate = 'submission_order_id' if 'submission_order_id' in columns else 'NULL'
                for config, result, payment_id, candidate_id in db.execute(
                        f'SELECT config,result,payment_order_id,{candidate} FROM tasks'):
                    hint = json.loads(config).get('address_hint', '')
                    if len(hint) >= 4 and '测试' not in hint: values.add(hint)
                    order = json.loads(result).get('order_id') if result else None
                    for value in (order, payment_id, candidate_id):
                        if value and len(value) >= 8: values.add(value)
    return [value.encode(encoding) for value in values for encoding in ('utf-8', 'utf-16-le')]


def check_bytes(name, raw, needles):
    if any(needle in raw for needle in needles):
        raise ValueError('Private local value found in public file: '+name)
    if re.search(rb'gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', raw):
        raise ValueError('Credential pattern found in public file: '+name)


def audit_source(needles):
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode('utf-8').split('\0')
    count = 0
    for name in filter(None, names):
        check_name(name, source=True)
        check_bytes(name, (ROOT/name).read_bytes(), needles)
        count += 1
    return count


def audit_bundle(archive, needles):
    count = 0
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            if item.is_dir(): continue
            check_name(item.filename)
            if '/data/' in item.filename and '/data/browsers/' not in item.filename:
                raise ValueError('Non-browser runtime data in release')
            check_bytes(item.filename, z.read(item), needles)
            count += 1
    return count


def audit_embedded(exe, needles):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(exe))
    count = 0
    for name, entry in archive.toc.items():
        if entry[-1] == 'z':
            pyz = archive.open_embedded_archive(name)
            for module in pyz.toc:
                raw = pyz.extract(module, raw=True)
                if raw is not None:  # Namespace packages have no bytecode.
                    check_bytes(module, raw, needles)
                    count += 1
        elif entry[-1] == 's':
            check_bytes(name, archive.extract(name), needles)
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', action='store_true')
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    needles = local_needles()
    result = {'private_values_included':False, 'known_local_encodings_checked':len(needles)}
    if args.source: result['source_files_checked'] = audit_source(needles)
    if args.bundle:
        result['archive_files_checked'] = audit_bundle(args.bundle, needles)
        bundle = args.bundle.with_suffix('')
        result['embedded_modules_checked'] = audit_embedded(bundle/'ShoppingAssistant.exe', needles)
    if args.report: args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__': main()
