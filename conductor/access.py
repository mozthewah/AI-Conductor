"""Who may use Conductor from other devices on your network.

Requests from this computer (127.0.0.1 / ::1) are always allowed. Any other device needs the access
passcode once; after that a long-lived cookie keeps it signed in. The passcode lives in
data/access_token (or CONDUCTOR_ACCESS_TOKEN) and is created the first time it's needed.

    http://192.168.1.20:8765/?key=<passcode>   -> signs this device in, then drops the key from the URL

Every state-changing request must also come from Conductor's own pages (Origin/Referer check), so a
website open in some other tab can't send commands to it.
"""

from __future__ import annotations

import html
import ipaddress
import os
import secrets
import socket
from pathlib import Path
from urllib.parse import quote, urlparse

COOKIE = "conductor_access"
COOKIE_AGE = 365 * 24 * 3600


def is_local(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return host in ("localhost", "testclient")


def load_token(data_dir: Path, create: bool = True) -> str:
    env = os.environ.get("CONDUCTOR_ACCESS_TOKEN", "").strip()
    if env:
        return env
    path = Path(data_dir) / "access_token"
    try:
        tok = path.read_text().strip()
        if tok:
            return tok
    except OSError:
        pass
    if not create:
        return ""
    tok = "-".join(secrets.token_hex(2) for _ in range(4))  # e.g. 3f9a-0c1e-77d2-b4a0: typeable on a phone
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tok + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return tok


def reset_token(data_dir: Path) -> str:
    """New passcode; every device has to sign in again."""
    try:
        (Path(data_dir) / "access_token").unlink()
    except OSError:
        pass
    return load_token(data_dir)


def lan_addresses() -> list[str]:
    """This computer's addresses on the local network (best effort, no external traffic)."""
    found: list[str] = []
    try:  # the address the default route would use; connect() on UDP sends nothing
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for a in found:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_private and not ip.is_loopback and a not in out:
            out.append(a)
    return out


def same_origin(origin_or_referer: str | None, host_header: str | None) -> bool:
    if not origin_or_referer:
        return True  # non-browser clients (curl, the CLI) send neither
    try:
        return urlparse(origin_or_referer).netloc == (host_header or "")
    except ValueError:
        return False


def qr_svg(text: str) -> str | None:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2, box_size=10)
    return img.to_string(encoding="unicode")


def qr_terminal(text: str) -> str | None:
    try:
        import io

        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=1)
    qr.add_data(text)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def login_page(next_url: str = "/", error: str = "") -> str:
    nxt = html.escape(next_url if next_url.startswith("/") and not next_url.startswith("//") else "/")
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Conductor - sign in</title>
<style>
:root {{ --bg:#efece4; --card:#f7f5ef; --ink:#1f1d19; --ink2:#5b574e; --line:#d9d4c7; --accent:#146b6b; --bad:#9a2c26; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141412; --card:#1c1c19; --ink:#e9e6dd; --ink2:#a39f94; --line:#2f2e29; --accent:#4fb0a5; --bad:#e0726b; }} }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink);
  font:16px/1.5 Inter, system-ui, sans-serif; padding:16px; box-sizing:border-box; }}
form {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:24px; width:100%; max-width:360px; }}
h1 {{ font:500 24px Newsreader, Georgia, serif; margin:0 0 6px; }}
p {{ color:var(--ink2); margin:0 0 16px; font-size:14px; }}
input {{ width:100%; box-sizing:border-box; font:inherit; font-size:18px; letter-spacing:.06em; padding:12px; border-radius:10px;
  border:1px solid var(--line); background:var(--bg); color:var(--ink); margin-bottom:12px; }}
button {{ width:100%; font:inherit; font-weight:600; padding:12px; border:0; border-radius:10px; background:var(--accent); color:#fff; }}
.err {{ color:var(--bad); }}
</style></head><body>
<form method="post" action="/login"><h1>Conductor</h1>
<p>Enter the access passcode. It's shown on the computer running Conductor (Safety page, or the terminal where you started it).</p>{err}
<input name="passcode" autocomplete="current-password" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="xxxx-xxxx-xxxx-xxxx" autofocus required>
<input type="hidden" name="next" value="{nxt}"><button>Sign in</button></form></body></html>"""


def device_link(token: str, port: int, host: str | None = None) -> str | None:
    host = host or next(iter(lan_addresses()), None)
    return f"http://{host}:{port}/?key={quote(token)}" if host else None
