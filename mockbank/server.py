"""MockBank Core — a deliberately "legacy" back-office app used as the automation target.

Hostile on purpose, like the real long tail: frameset layout, table-based forms, no ids or
test ids, unlabeled inputs with generic names (f1, f2), status messages as colored text.
All data is synthetic.

Data-driven exceptional states:
    99999        -> E404 no member on file          (business outcome)
    00000 / bad  -> E102 invalid member number      (business outcome: validation)
    24680        -> E403 restricted member record   (business outcome: permission denied)
    13579        -> supervisor override required   (needs a human)

Injectable faults (counts, consumed as they fire), e.g.
    GET /__faults?interstitial=1&slow=1&slow_ms=3000&error500=1&expire=1&confirm=1   (&reset=1 zeroes all)
"""

import argparse
import html
import json
import os
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

VERSION = "2.4.1"
USER = os.environ.get("MOCKBANK_USER", "teller1")
PASSWORD = os.environ.get("MOCKBANK_PASSWORD", "teller-pass")
OVERRIDE_CODE = os.environ.get("MOCKBANK_OVERRIDE_CODE", "4242")
IDLE_TIMEOUT_S = 600

MEMBERS = {
    "12345": {"name": "Jordan A. Rivera", "since": "2014-03-02", "ssn4": "4821", "accounts": [
        ("Share Savings", "S-0012345-01", "$4,210.55"),
        ("Share Draft Checking", "D-0012345-02", "$1,032.10"),
        ("Auto Loan", "L-0012345-03", "($8,400.00)")]},
    "54321": {"name": "Priya N. Shah", "since": "2019-11-17", "ssn4": "0917", "accounts": [
        ("Share Savings", "S-0054321-01", "$17,880.02"),
        ("Money Market", "M-0054321-04", "$25,000.00")]},
    "13579": {"name": "Casey M. Lee", "since": "2021-06-30", "ssn4": "3366", "flagged": True, "accounts": [
        ("Share Savings", "S-0013579-01", "$602.19")]},
    "24680": {"name": "Morgan T. Blake", "since": "2008-01-09", "ssn4": "7710", "restricted": True, "accounts": []},
}

SESSIONS: dict[str, dict] = {}
FAULTS = {"interstitial": 0, "slow": 0, "slow_ms": 3000, "error500": 0, "expire": 0, "confirm": 0}
LOCK = threading.Lock()


def take(fault: str) -> bool:
    """Consume one occurrence of a fault if armed."""
    with LOCK:
        if FAULTS[fault] > 0:
            FAULTS[fault] -= 1
            return True
    return False


def page(body: str) -> str:
    return (f'<html><head><title>MockBank Core</title></head><body bgcolor="#e8e8e8">'
            f'<font face="Arial" size="2">{body}</font>'
            f'<hr><font size="1" color="#666">MockBank Core v{VERSION}</font></body></html>')


def banner(text: str) -> str:
    return (f'<table width="100%" cellpadding="3"><tr><td bgcolor="#003366">'
            f'<font color="white" size="4"><b>{text}</b></font></td></tr></table><br>')


LOGIN_FORM = """{banner}{msg}
<form method="post" action="/login"><table>
<tr><td>User ID</td><td><input name="u"></td></tr>
<tr><td>Password</td><td><input type="password" name="p"></td></tr>
<tr><td></td><td><input type="submit" value="Sign On"></td></tr></table></form>"""

FRAMESET = """<html><head><title>MockBank Core</title></head>
<frameset rows="40,*" border="0"><frame name="menu" src="/menu"><frame name="main" src="/home"></frameset></html>"""

MENU = """<a href="/inquiry" target="main">Member Inquiry</a> | <a href="/transfers" target="main">Transfers</a>
| <a href="/logout" target="_top">Sign Off</a>"""

INTERSTITIAL = """<div style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.5)">
<div style="background:#ffc;margin:60px auto;width:360px;padding:12px;border:2px solid #000">
<b>SYSTEM NOTICE</b><p>Scheduled maintenance tonight 22:00-23:00 ET. Save your work.</p>
<button onclick="this.parentNode.parentNode.remove()">Acknowledge</button></div></div>"""

EXPIRED = page('<font color="red"><b>Your session has expired.</b></font> Please sign on again. '
               '<a href="/login" target="_top">Sign On</a>')


def inquiry_form(msg: str = "") -> str:
    msg = f'<font color="red"><b>{msg}</b></font><br><br>' if msg else ""
    return page(banner("Member Inquiry") + msg + """<form method="post" action="/mbr"><table>
<tr><td>Member #</td><td><input name="f1" size="8"></td><td><input type="submit" value="Go"></td></tr>
</table></form>""")


def detail(mid: str) -> str:
    m = MEMBERS[mid]
    rows = "".join(f'<tr><td>{t}</td><td>{a}</td><td align="right">{b}</td></tr>' for t, a, b in m["accounts"])
    return page(banner("Member Detail") + f"""<table border="1" cellspacing="0" cellpadding="3">
<tr><td>Member #</td><td>{mid}</td></tr><tr><td>Name</td><td>{m['name']}</td></tr>
<tr><td>SSN</td><td>***-**-{m['ssn4']}</td></tr><tr><td>Member Since</td><td>{m['since']}</td></tr></table>
<br><b>Accounts</b><table border="1" cellspacing="0" cellpadding="3">
<tr bgcolor="#cccccc"><td>Type</td><td>Account</td><td>Balance</td></tr>{rows}</table><br>
<form method="post" action="/close"><input type="hidden" name="f1" value="{mid}">
<input type="submit" value="Close Account"></form><a href="/inquiry">New Inquiry</a>""")


def override_form(mid: str, msg: str = "") -> str:
    msg = f'<font color="red"><b>{msg}</b></font><br>' if msg else ""
    return page(banner("SUPERVISOR OVERRIDE REQUIRED") + msg + f"""Member record is flagged.
A supervisor must authorize access.<form method="post" action="/override">
<input type="hidden" name="f1" value="{html.escape(mid)}"><table>
<tr><td>Override Code</td><td><input type="password" name="f2"></td></tr></table>
<input type="submit" value="Authorize"></form>""")


class Handler(BaseHTTPRequestHandler):
    server_version = f"MockBankCore/{VERSION}"

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method: str) -> None:
        url = urlsplit(self.path)
        raw = url.query
        if method == "POST":
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        f = {k: v[0] for k, v in parse_qs(raw).items()}
        path = url.path

        if path == "/__faults":
            return self.faults(f)
        if path == "/login":
            return self.login(method, f)
        sid = SimpleCookie(self.headers.get("Cookie", "")).get("SID")
        sid = sid.value if sid else ""
        if path == "/logout":
            SESSIONS.pop(sid, None)
            return self.redirect("/login", [("Set-Cookie", "SID=; Path=/; Max-Age=0")])
        sess = SESSIONS.get(sid)
        if sess and (take("expire") or time.time() - sess["last"] > IDLE_TIMEOUT_S):
            SESSIONS.pop(sid, None)
            sess = None
        if sess is None:  # never signed on → sign-on page; had a session → "expired" page
            return self.redirect("/login") if path == "/" and not sid else self.send(200, EXPIRED)
        sess["last"] = time.time()

        if path == "/":
            return self.send(200, FRAMESET)
        if path == "/menu":
            return self.send(200, page(MENU))
        if path == "/home":
            return self.send(200, page(banner("Welcome") + f"Signed on as {USER}. Select a function from the menu."))
        if path == "/transfers":
            return self.send(200, page(banner("Transfers") +
                                       '<font color="red"><b>E403 ACCESS DENIED - INSUFFICIENT PRIVILEGES</b></font>'))
        if path == "/inquiry":
            self.maybe_slow()
            body = inquiry_form()
            if take("interstitial"):
                body = body.replace("<hr>", INTERSTITIAL + "<hr>", 1)
            if take("confirm"):  # a native confirm() the recorded flow never saw
                body = body.replace('action="/mbr"', 'action="/mbr" onsubmit="return confirm('
                                    "'This member record is open at another teller station. Continue?')\"", 1)
            return self.send(200, body)
        if path == "/mbr":
            self.maybe_slow()
            if take("error500"):
                return self.send(500, page("<h2>Internal Server Error</h2>Transaction aborted (ref 0x1F3A)."))
            return self.member(sess, f.get("f1", "").strip())
        if path == "/override":
            mid = f.get("f1", "")
            if mid in MEMBERS and f.get("f2") == OVERRIDE_CODE:
                sess["overrides"].add(mid)
                return self.send(200, detail(mid))
            return self.send(200, override_form(mid, "INVALID OVERRIDE CODE"))
        if path == "/close" and method == "POST":
            return self.send(200, page(banner("Account Closure") + "Closure request submitted. This cannot be undone."))
        return self.send(404, page("<h2>Not Found</h2>"))

    def member(self, sess: dict, mid: str) -> None:
        if not re.fullmatch(r"\d{5}", mid) or mid == "00000":
            return self.send(200, inquiry_form("E102 INVALID MEMBER NUMBER"))
        m = MEMBERS.get(mid)
        if m is None:
            return self.send(200, inquiry_form("E404 NO MEMBER ON FILE"))
        if m.get("restricted"):
            return self.send(200, page(banner("Member Inquiry") +
                                       '<font color="red"><b>E403 ACCESS DENIED - RESTRICTED MEMBER RECORD</b></font>'))
        if m.get("flagged") and mid not in sess["overrides"]:
            return self.send(200, override_form(mid))
        return self.send(200, detail(mid))

    def login(self, method: str, f: dict) -> None:
        msg = ""
        if method == "POST":
            if f.get("u") == USER and f.get("p") == PASSWORD:
                sid = secrets.token_urlsafe(16)
                SESSIONS[sid] = {"last": time.time(), "overrides": set()}
                return self.redirect("/", [("Set-Cookie", f"SID={sid}; Path=/; HttpOnly")])
            msg = '<font color="red"><b>INVALID SIGN-ON</b></font><br>'
        self.send(200, page(LOGIN_FORM.format(banner=banner("MockBank Core — Sign On"), msg=msg)))

    def faults(self, f: dict) -> None:
        with LOCK:
            if f.pop("reset", None):
                FAULTS.update(interstitial=0, slow=0, error500=0, expire=0, confirm=0)
            for k, v in f.items():
                if k in FAULTS:
                    FAULTS[k] = int(v)
            body = json.dumps(FAULTS)
        self.send(200, body, content_type="application/json")

    def maybe_slow(self) -> None:
        if take("slow"):
            time.sleep(FAULTS["slow_ms"] / 1000)

    def redirect(self, to: str, headers=()) -> None:
        self.send(302, "", [("Location", to), *headers])

    def send(self, status: int, body: str, headers=(), content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


def make_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    global IDLE_TIMEOUT_S
    ap = argparse.ArgumentParser(description="Run the MockBank Core target app.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--idle-timeout", type=int, default=IDLE_TIMEOUT_S, help="session idle timeout, seconds")
    args = ap.parse_args()
    IDLE_TIMEOUT_S = args.idle_timeout
    srv = make_server(port=args.port)
    print(f"MockBank Core v{VERSION} on http://127.0.0.1:{args.port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
