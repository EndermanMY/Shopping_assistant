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


def test_candidate_order_id_is_scanned_before_final_confirmation(tmp_path, monkeypatch):
    from scripts import audit_publication
    from assistant.store import Store
    monkeypatch.setattr(audit_publication, 'ROOT', tmp_path)
    store = Store(tmp_path/'data-api'/'assistant.db')
    # A response-only ID must be protected even with no result/payment ID.
    with store.db:
        store.db.execute("INSERT INTO tasks(id,config,status,created_at,updated_at,submission_order_id) VALUES(?,?,?,?,?,?)",
                         ('fixture', '{}', 'waiting_human', '', '', '98765432101234567890'))
    needles = audit_publication.local_needles()
    assert b'98765432101234567890' in needles
    with pytest.raises(ValueError):
        check_bytes('example.py', b'98765432101234567890', needles)
    store.close()
