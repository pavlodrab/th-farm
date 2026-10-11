#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TokenHarbor — self-contained регистратор для GitHub Actions runner.

Один запуск = один аккаунт на свежем Azure-IP раннера (капчу обычно не
спрашивают). Почта — внешний apimail (catch-all, админ-API), verify-ссылка
читается оттуда. Результат печатается одной JSON-строкой в stdout и
дублируется в ./out/<email>.json для артефакта.

ENV:
  MAIL_BASE       — база apimail, напр. https://apimail.example.com
  MAIL_ADMIN_KEY  — x-admin-auth ключ
  MAIL_DOMAIN     — catch-all домен, напр. example.com
  TH_PROXY        — (опц.) http://user:pass@host:port; пусто = напрямую
  MAIL_TIMEOUT    — сколько ждать письмо, сек (по умолчанию 180)
"""
import base64
import html
import json
import os
import random
import re
import secrets
import string
import sys
import time
import uuid
from urllib.parse import urlencode

import requests

BASE = "https://tokenharbor.ai"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

MAIL_BASE = os.environ.get("MAIL_BASE", "").rstrip("/")
MAIL_KEY = os.environ.get("MAIL_ADMIN_KEY", "")
MAIL_DOMAIN = os.environ.get("MAIL_DOMAIN", "")
TH_PROXY = os.environ.get("TH_PROXY") or None
MAIL_TIMEOUT = int(os.environ.get("MAIL_TIMEOUT", "180"))

_ACTION_INPUT_RE = re.compile(r'name="\$ACTION_1:0" value="([^"]+)"')
_DPL_RE = re.compile(r"dpl_[A-Za-z0-9]{8,}")
_LINK_RES = [
    re.compile(r"https://tokenharbor\.ai/verify[^\s\"'<>\\)]+", re.I),
    re.compile(r"https://tokenharbor\.ai/[^\s\"'<>\\)]*token=[^\s\"'<>\\)]+", re.I),
]


def log(msg):
    print(f"  [th] {msg}", flush=True)


def gen_password(n=16):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    pw = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
          secrets.choice(string.digits), secrets.choice("!@#$%^&*")]
    pw += [secrets.choice(chars) for _ in range(n - 4)]
    secrets.SystemRandom().shuffle(pw)
    return "".join(pw)


def proxies():
    return {"http": TH_PROXY, "https": TH_PROXY} if TH_PROXY else None


# ── apimail ──────────────────────────────────────────────────────────────
def mail_address():
    local = "th" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"{local}@{MAIL_DOMAIN}"


def mail_link(address, since_ts, timeout=MAIL_TIMEOUT):
    """Ждёт verify-ссылку target-а в catch-all ящике через админ-API."""
    import email as em
    headers = {"x-admin-auth": MAIL_KEY, "User-Agent": "curl/8.5.0",
               "Accept": "application/json"}
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        try:
            r = requests.get(f"{MAIL_BASE}/admin/mails",
                             params={"address": address, "limit": 15, "offset": 0},
                             headers=headers, timeout=25)
            mails = r.json().get("results", []) if r.status_code == 200 else []
        except Exception as exc:
            log(f"mail list: {str(exc)[:100]}")
            mails = []
        for m in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
            if m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            msg = em.message_from_string(m.get("raw", "") or "")
            frm = str(msg.get("From", "")) + str(msg.get("Subject", ""))
            if "tokenharbor" not in frm.lower():
                continue
            if since_ts:
                try:
                    ts = em.utils.mktime_tz(em.utils.parsedate_tz(msg.get("Date", "")))
                    if ts and ts < since_ts - 60:
                        continue
                except Exception:
                    pass
            body = ""
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    p = part.get_payload(decode=True)
                    if p:
                        body += p.decode("utf-8", "ignore") + "\n"
            body = body.replace("&amp;", "&")
            for rx in _LINK_RES:
                mm = rx.search(body)
                if mm:
                    return mm.group(0).rstrip(".,;>]")
        log(f"письма пока нет ({int(deadline - time.time())}s)")
        time.sleep(5)
    return None


# ── tokenharbor server actions ───────────────────────────────────────────
class Client:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Origin": BASE})
        if TH_PROXY:
            self.s.proxies = {"http": TH_PROXY, "https": TH_PROXY}
        self.dpl = None
        self.action = None

    def discover(self):
        r = self.s.get(f"{BASE}/login?mode=signup", timeout=25)
        r.raise_for_status()
        m = _DPL_RE.search(r.headers.get("Link", "") or "") or _DPL_RE.search(r.text)
        if m:
            self.dpl = m.group(0)
        am = _ACTION_INPUT_RE.search(r.text)
        if not am:
            raise RuntimeError("action id не найден — верстка сменилась")
        self.action = json.loads(html.unescape(am.group(1)))["id"]
        return self.action

    def _multipart(self, fields):
        bd = "----WebKitFormBoundary" + uuid.uuid4().hex[:16]
        sep = f"--{bd}"
        parts = [
            f'{sep}\r\nContent-Disposition: form-data; name="1_$ACTION_REF_1"\r\n\r\n\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_$ACTION_1:0"\r\n\r\n'
            f'{{"id":"{self.action}","bound":"$@1"}}\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_$ACTION_1:1"\r\n\r\n["$undefined"]\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_$ACTION_KEY"\r\n\r\n'
            f'k{uuid.uuid4().hex[:31]}\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_device_fingerprint"\r\n\r\n'
            f'{uuid.uuid4()}\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_timezone"\r\n\r\nAmerica/New_York\r\n',
            f'{sep}\r\nContent-Disposition: form-data; name="1_next"\r\n\r\n\r\n',
        ]
        for n, v in fields:
            parts.append(f'{sep}\r\nContent-Disposition: form-data; name="1_{n}"\r\n\r\n{v}\r\n')
        parts.append(f'{sep}\r\nContent-Disposition: form-data; name="0"\r\n\r\n["$undefined","$K1"]\r\n')
        parts.append(f"{sep}--\r\n")
        return "".join(parts).encode(), bd

    def call(self, fields):
        body, bd = self._multipart(fields)
        page = f"{BASE}/login?mode=signup"
        r = self.s.post(page, headers={
            "Content-Type": f"multipart/form-data; boundary={bd}",
            "next-action": self.action,
            "Accept": "text/x-component", "Referer": page,
            "x-deployment-id": self.dpl or "",
        }, data=body, timeout=40)
        r.encoding = "utf-8"
        for line in r.text.strip().split("\n"):
            try:
                _, data = line.split(":", 1)
                parsed = json.loads(data)
            except Exception:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), str) \
                    and not parsed["error"].startswith("$"):
                return {"ok": False, "error": parsed["error"]}
        if r.status_code in (200, 303):
            return {"ok": True}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}

    def create_key(self):
        r = self.s.post(f"{BASE}/api/keys", json={"label": "gh-runner"}, timeout=25)
        if not r.ok:
            return None
        return r.json().get("plaintext") or (r.json().get("key") or {}).get("plaintext")

    def enable_free(self):
        try:
            self.s.post(f"{BASE}/api/me/privacy",
                        json={"consent_version": 2, "free_models_enabled": True}, timeout=20)
        except Exception:
            pass


def main():
    if not (MAIL_BASE and MAIL_KEY and MAIL_DOMAIN):
        print(json.dumps({"error": "MAIL_BASE/MAIL_ADMIN_KEY/MAIL_DOMAIN не заданы"}))
        return 2
    email = mail_address()
    password = gen_password()
    started = time.time()
    log(f"аккаунт {email}")

    c = Client()
    try:
        c.discover()
        res = c.call([("email", email), ("password", password), ("invite_code", "")])
    except Exception as exc:
        print(json.dumps({"email": email, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    if not res.get("ok"):
        print(json.dumps({"email": email, "error": f"signup: {res.get('error')}"}))
        return 1

    log("signup ок, жду письмо...")
    link = mail_link(email, since_ts=started)
    if not link:
        print(json.dumps({"email": email, "error": "verify-письмо не пришло"}))
        return 1
    try:
        c.s.get(link, allow_redirects=True, timeout=25)
    except Exception as exc:
        print(json.dumps({"email": email, "error": f"verify GET: {exc}"}))
        return 1

    key = c.create_key()
    if not key:
        print(json.dumps({"email": email, "error": "ключ не создан"}))
        return 1
    c.enable_free()

    out = {"email": email, "password": password, "api_key": key,
           "free_tier_enabled": True,
           "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    os.makedirs("out", exist_ok=True)
    with open(os.path.join("out", f"{email}.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False) + "\n")
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())