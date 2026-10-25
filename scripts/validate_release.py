"""Verify the exact ZIP contents, then bind the EXE smoke report to its hash."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('bundle',type=Path)
    args=parser.parse_args();bundle=args.bundle.resolve()
    archive=bundle.parent/(bundle.name+'.zip')
    prefix=bundle.name+'/'
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        names=z.namelist();manifest=json.loads(z.read(prefix+'SHA256SUMS.json'))
        for name,digest in manifest.items():
            assert hashlib.sha256(z.read(prefix+name)).hexdigest()==digest,name
        assert all(n.startswith(prefix+'data/browsers/') for n in names if n.startswith(prefix+'data/'))
        for secret in ('access.json','assistant.db','browser/jd','server.log','preflight.json'):
            assert not any(secret in n for n in names),secret
        version=json.loads(z.read(prefix+'version.json'))['version']
        assert any('/licenses/native/libffi-' in n for n in names)
        exe_hash=hashlib.sha256(z.read(prefix+'ShoppingAssistant.exe')).hexdigest()
        assert exe_hash==hashlib.sha256((bundle/'ShoppingAssistant.exe').read_bytes()).hexdigest()
    checksum=hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksum==archive.with_suffix('.zip.sha256').read_text().split()[0]
    root=Path(__file__).resolve().parent.parent
    report=json.loads((root/'data/qa/release-smoke.json').read_text(encoding='utf-8'))
    assert report['version']==version
    report.update(archive_sha256=checksum,executable_sha256=exe_hash,files=len(names),manifest_verified=len(manifest),archive_crc_ok=True,private_data_excluded=True,size_bytes=archive.stat().st_size)
    (archive.parent/f'v{version}-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    main()
