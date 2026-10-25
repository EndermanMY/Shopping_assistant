"""Assemble a private-data-free portable release from explicit allowlists."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True)
    parser.add_argument('--finalize', action='store_true', help='Refresh documentation/licenses and checksums on an unpublished build')
    args = parser.parse_args()
    name = f'ShoppingAssistant-{args.version}-windows-x64'
    target = ROOT / 'release' / name
    executable = target / f'{name}.exe'
    if not args.finalize:
        executable.rename(target / 'ShoppingAssistant.exe')
    else:
        assert (target / 'ShoppingAssistant.exe').is_file()
    (target / 'start.bat').write_text('@echo off\ncd /d "%~dp0"\nShoppingAssistant.exe\npause\n', encoding='ascii')
    for filename in ('README.md', 'THIRD_PARTY.md', 'requirements.lock.txt', 'requirements-build.lock.txt', 'enable-lan.ps1'):
        shutil.copy2(ROOT / filename, target / filename)
    shutil.copytree(ROOT / 'docs', target / 'docs', dirs_exist_ok=args.finalize)
    shutil.copy2(ROOT / 'docs' / f'RELEASE-v{args.version}.md', target / 'READ-ME-FIRST.md')
    browser_dest = target / 'data' / 'browsers'
    for folder in (ROOT / 'data' / 'browsers').iterdir():
        if folder.is_dir() and folder.name.startswith(('chromium-', 'ffmpeg-', 'winldd-')):
            if not (args.finalize and (browser_dest / folder.name).is_dir()):
                shutil.copytree(folder, browser_dest / folder.name)
    chromium = list(browser_dest.glob('chromium-*/chrome-win*/chrome.exe'))
    assert len(chromium) == 1, 'One bundled Chromium is required'

    licenses = target / 'licenses'
    index = []
    for dist in importlib.metadata.distributions():
        package = dist.metadata['Name']
        if package in ('shopping-assistant', 'pip'):
            continue
        copied = []
        for file in dist.files or []:
            if file.name.lower().startswith(('license', 'copying', 'notice')):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    safe_parts = [part if part not in ('..', '.') else '_' for part in file.parts]
                    dest = licenses / 'python' / package / Path(*safe_parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
                    copied.append(str(dest.relative_to(target)))
        index.append({'package': package, 'version': dist.version, 'license': dist.metadata.get('License-Expression') or dist.metadata.get('License'), 'files': copied})
    for source in (ROOT / 'frontend' / 'node_modules').rglob('*'):
        if source.is_file() and source.name.lower().startswith(('license', 'copying', 'notice')):
            dest = licenses / 'npm' / source.relative_to(ROOT / 'frontend' / 'node_modules')
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    for source in (Path(sys.base_prefix) / 'LICENSE.txt', Path(sys.base_prefix) / 'LICENSE', Path(sys.base_prefix) / 'LICENSE_PYTHON.txt'):
        if source.is_file():
            shutil.copy2(source, licenses / 'CPython-LICENSE.txt')
    for package in ('libffi', 'sqlite', 'openssl'):
        for source in (Path(sys.base_prefix)/'pkgs').glob(f'{package}-*/info/licenses/*'):
            if source.is_file():
                dest = licenses/'native'/source.parent.parent.parent.name/source.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=str(chromium[0]), headless=True)
        page = browser.new_page()
        page.goto('chrome://credits/')
        credits = page.content()
        assert 'Chromium' in credits and 'license' in credits.lower()
        (licenses / 'Chromium-credits.html').write_text(credits, encoding='utf-8')
        browser.close()
    (licenses / 'python-packages.json').write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding='utf-8')
    (target / 'version.json').write_text(json.dumps({'version': args.version, 'platform': 'windows-x64', 'python': sys.version.split()[0], 'format': 'portable-onedir'}, indent=2), encoding='utf-8')
    hashes = {str(p.relative_to(target)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(target.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS.json'}
    (target / 'SHA256SUMS.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
    # Runtime data is never copied: data contains only pristine browser binaries.
    assert {p.name for p in (target / 'data').iterdir()} == {'browsers'}
    archive = ROOT / 'release' / f'{name}.zip'
    working_archive = archive.with_suffix('.zip.finalizing') if args.finalize else archive
    with zipfile.ZipFile(working_archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(target.rglob('*')):
            if p.is_file():
                z.write(p, Path(name) / p.relative_to(target))
    if args.finalize:
        os.replace(working_archive, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    print(json.dumps({'archive': str(archive), 'bytes': archive.stat().st_size, 'sha256': digest}))


if __name__ == '__main__':
    main()
