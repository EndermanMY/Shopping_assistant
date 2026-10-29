import pytest

from scripts.audit_publication import check_bytes, check_name


@pytest.mark.parametrize('name', ['data/access.json', 'data/browser/jd/Default/Network/Cookies',
                                  'data-api/assistant.db', '.env', '.venv/token', 'notes/private.log'])
def test_private_files_are_rejected(name):
    with pytest.raises(ValueError): check_name(name, source=True)


def test_current_local_secret_is_rejected_without_echoing_value():
    with pytest.raises(ValueError) as error:
        check_bytes('example.py', b'prefix-private-test-value-suffix', [b'private-test-value'])
    assert 'private-test-value' not in str(error.value)


def test_public_source_and_bundle_runtime_are_allowed():
    check_name('assistant/browser.py', source=True)
    check_name('ShoppingAssistant/data/browsers/chrome-stable/chrome.exe')
    check_name('ShoppingAssistant/_internal/certifi/cacert.pem')
    check_bytes('example.py', b'print("No login information")', [b'private-test-value'])
