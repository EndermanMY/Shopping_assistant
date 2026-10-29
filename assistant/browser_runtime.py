"""Resolve only a pinned, project-bundled browser. Never use system Chrome."""
import json
import re
from pathlib import Path


def bundled_executable(root: Path) -> Path | None:
    manifest = root / 'runtime.json'
    if not manifest.exists():
        return None  # Playwright's bundled Chromium remains the fallback.
    config = json.loads(manifest.read_text(encoding='utf-8-sig'))
    relative = config['relative_path']
    if (not isinstance(relative, str) or ':' in relative or relative.startswith(('/', '\\'))
            or Path(relative).is_absolute() or '..' in Path(relative).parts):
        raise ValueError('Invalid bundled browser path')
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or target.name != 'chrome.exe' or not target.is_file():
        raise ValueError('Bundled browser is missing or outside its directory')
    return target


def session_settings(data: Path):
    """Local opt-in only; never infer that system proxy should be bypassed."""
    path = data/'browser-network.json'
    config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    mode = config.get('mode', 'system')
    name = config.get('profile_name', 'jd')
    if mode not in ('system', 'direct') or not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', name):
        raise ValueError('Invalid independent browser settings')
    root = (data/'browser').resolve()
    profile = (root/name).resolve()
    if not profile.is_relative_to(root):
        raise ValueError('Browser session must stay inside the app data directory')
    return profile, mode


def connection_setting(data: Path) -> str:
    path = data/'browser-network.json'
    config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    mode = config.get('connection_mode', 'native_cdp')
    if mode not in {'native_cdp', 'persistent'}:
        raise ValueError('Invalid independent browser connection mode')
    return mode
