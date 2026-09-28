"""Smoke test for the target app's states and fault injection (HTTP level, no browser)."""

import threading
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from urllib.parse import urlencode

import pytest

from mockbank import server


@pytest.fixture(scope="module")
def base():
    srv = server.make_server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def client(base):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(path, **form):
        data = urlencode(form).encode() if form else None
        return opener.open(base + path, data).read().decode()

    assert "<frameset" in call("/login", u=server.USER, p=server.PASSWORD)
    return call


def test_lookup_and_business_states(base):
    call = client(base)
    assert "Member Detail" in call("/mbr", f1="12345") and "$4,210.55" in call("/mbr", f1="12345")
    assert "E404" in call("/mbr", f1="99999")
    assert "E102" in call("/mbr", f1="00000") and "E102" in call("/mbr", f1="12a45")
    assert "E403" in call("/mbr", f1="24680")
    assert "SUPERVISOR OVERRIDE" in call("/mbr", f1="13579")
    assert "INVALID OVERRIDE" in call("/override", f1="13579", f2="0000")
    assert "Member Detail" in call("/override", f1="13579", f2=server.OVERRIDE_CODE)
    assert "Member Detail" in call("/mbr", f1="13579")  # override sticks for the session


def test_no_session_is_expired(base):
    opener = urllib.request.build_opener()
    assert "session has expired" in opener.open(base + "/inquiry").read().decode()


def test_faults_fire_once(base):
    call = client(base)
    call("/__faults", reset=1, interstitial=1, error500=1, expire=1)
    assert "session has expired" in call("/inquiry")  # expire fires first, on any authed request
    call = client(base)
    assert "SYSTEM NOTICE" in call("/inquiry")
    assert "SYSTEM NOTICE" not in call("/inquiry")
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/mbr", f1="12345")
    assert e.value.code == 500
    assert "Member Detail" in call("/mbr", f1="12345")
