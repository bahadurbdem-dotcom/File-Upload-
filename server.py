#!/usr/bin/env python3
"""
Ay File Host — Telegram-backed file hosting server (Render Web Service ready).

* ফাইল সরাসরি টেলিগ্রাম গ্রুপে স্ট্রিম হয় (MTProto / Telethon) — Render-এ কিছুই সেভ হয় না।
* ডাউনলোড টেলিগ্রাম থেকে স্ট্রিম হয়ে আসে (Range সাপোর্ট: ভিডিও সিক, রিজিউম)।
* লিংক:  https://your-app.onrender.com/f/<id>-<sig>/<filename.ext>
* API ID, API Hash, Bot Token, Group ID, পাসওয়ার্ড, সব সেটিং — সবই অ্যাডমিন প্যানেল থেকে বদলানো যায়।
  কোডে কোনো সিক্রেট লেখা নেই।

সেটিংসের দুইটি স্তর:
  1. CORE  (api_id, api_hash, token, group_id, পাসওয়ার্ড হ্যাশ, public_url, render key)
       → Environment Variable  +  data/state.json  (+ Render API থাকলে Render Environment-এ অটো-সেভ)
  2. SOFT  (সাইটের নাম, সাইজ লিমিট, লিংক সিক্রেট, API কী, সিকিউরিটি অপশন …)
       → টেলিগ্রাম গ্রুপের পিন করা এনক্রিপ্টেড মেসেজ  (রিস্টার্টেও হারায় না)
"""
import asyncio
import base64
import collections
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import sys
import time
from urllib.parse import quote, unquote, urlparse

import aiohttp
from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import StringSession

VERSION = "2.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR") or os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
PORT = int(os.getenv("PORT", "10000"))
CHUNK = 512 * 1024
MARK = "#AYHOST_CONFIG"
STARTED = time.time()

# ------------------------------------------------------------------ logging (প্যানেলে লগ দেখানোর জন্য রিং বাফার)
LOGS = collections.deque(maxlen=200)


class RingHandler(logging.Handler):
    def emit(self, record):
        try:
            LOGS.append({"t": int(record.created), "l": record.levelname, "m": self.format(record)})
        except Exception:
            pass


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_rh = RingHandler()
_rh.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_rh)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("ayhost")

for _ext, _mt in {
    ".apk": "application/vnd.android.package-archive",
    ".xapk": "application/octet-stream",
    ".apks": "application/octet-stream",
    ".webp": "image/webp",
    ".mkv": "video/x-matroska",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".json": "application/json",
}.items():
    mimetypes.add_type(_mt, _ext)

# ------------------------------------------------------------------ settings model

# CORE: কীগুলোর Environment নাম
CORE_ENV = {
    "api_id": "API_ID",
    "api_hash": "API_HASH",
    "token": "BOT_TOKEN",
    "group_id": "GROUP_ID",
    "pw": "ADMIN_PW_HASH",
    "public_url": "PUBLIC_URL",
    "render_key": "RENDER_API_KEY",
    "render_service": "RENDER_SERVICE_ID",
}

# SOFT: ডিফল্ট মান
DEFAULTS = {
    "site_name": "Ay File Host",
    "accent": "#4f8cff",
    "max_size_mb": 1900,
    "html_sandbox": True,
    "api_upload": True,
    "caption": True,
    "blocked_ext": "",
    "concurrency": 3,
    "cache_seconds": 300,
    "session_days": 7,
    "login_max_fails": 5,
    "lockout_minutes": 10,
    "keepalive": True,
    "link_secret": "",
    "api_key": "",
}

# প্যানেল থেকে বদলানো যায় এমন সেটিংসের নিয়ম: কী -> (টাইপ, মিন, ম্যাক্স)
SCHEMA = {
    "site_name": ("str", 1, 60),
    "accent": ("color",),
    "max_size_mb": ("int", 1, 1999),
    "html_sandbox": ("bool",),
    "api_upload": ("bool",),
    "caption": ("bool",),
    "blocked_ext": ("exts",),
    "concurrency": ("int", 1, 10),
    "cache_seconds": ("int", 0, 31536000),
    "session_days": ("int", 1, 90),
    "login_max_fails": ("int", 3, 20),
    "lockout_minutes": ("int", 1, 1440),
    "keepalive": ("bool",),
}


class S:  # global state
    pass


ST = S()
ST.core = {}
ST.cfg = dict(DEFAULTS)
ST.client = None
ST.entity = None
ST.error = ""
ST.bot = ""
ST.group_title = ""
ST.cfg_mid = None
ST.top = 0
ST.boot_secret = secrets.token_hex(16)
ST.lock = None
ST.sem = None
ST.meta = {}
ST.stats = (0, None)
ST.fails = {}
ST.sync_msg = ""
ST.counters = {"uploads": 0, "upload_bytes": 0, "downloads": 0, "download_bytes": 0}
PAGES = {}


# ------------------------------------------------------------------ small helpers


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024


def jr(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def safe_name(name, mime=""):
    name = os.path.basename((name or "file").replace("\\", "/")).strip().replace(" ", "_")
    name = re.sub(r'[:*?"<>|\x00-\x1f]', "_", name) or "file"
    if not os.path.splitext(name)[1]:
        name += mimetypes.guess_extension(mime or "") or ""
    root, ext = os.path.splitext(name)
    return root[: 150 - len(ext)] + ext


def mask(v, keep=4):
    v = str(v or "")
    if not v:
        return ""
    if len(v) <= keep * 2 + 2:
        return "•" * len(v)
    return v[:keep] + "•••" + v[-keep:]


def cget(key, default=""):
    return ST.core.get(key) or default


# ------------------------------------------------------------------ CORE store (env + file + Render sync)


def core_load():
    core = {}
    for k, env in CORE_ENV.items():
        v = os.getenv(env, "").strip()
        if v:
            core[k] = v
    if not core.get("public_url") and os.getenv("RENDER_EXTERNAL_URL"):
        core["_auto_url"] = os.getenv("RENDER_EXTERNAL_URL").rstrip("/")
    try:  # ফাইলে থাকা মান (প্যানেলে সেট করা) Environment-কে ওভাররাইড করে
        with open(STATE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in (saved.get("core") or {}).items():
            if v:
                core[k] = str(v)
        ST.cfg = {**DEFAULTS, **{k: v for k, v in (saved.get("cfg") or {}).items() if k in DEFAULTS}}
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("state file unreadable: %s", e)
    if not core.get("sess_key"):
        core["sess_key"] = secrets.token_hex(24)
    ST.core = core


def state_write():
    """core + cfg মিরর লোকাল ফাইলে লেখা (0600)। ফেইল করলে False।"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        data = {"core": {k: v for k, v in ST.core.items() if not k.startswith("_")}, "cfg": ST.cfg}
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
        return True
    except Exception as e:
        log.warning("state file write failed: %s", e)
        return False


def render_ready():
    return bool(cget("render_key") and cget("render_service"))


async def render_sync(force=False):
    """core-এর মান Render Environment-এ সেভ করে (যাতে রিস্টার্ট/রিডিপ্লয়ে হারায় না)।"""
    if not render_ready():
        ST.sync_msg = "Render API Key সেট নেই"
        return False
    pairs = {}
    for k, env in CORE_ENV.items():
        v = cget(k)
        if v and (force or os.getenv(env, "") != v):
            pairs[env] = v
    if not pairs:
        ST.sync_msg = "সব মান Render-এ আগে থেকেই সেভ আছে"
        return True
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            for env, v in pairs.items():
                async with s.put(
                    f"https://api.render.com/v1/services/{cget('render_service')}/env-vars/{env}",
                    json={"value": v},
                    headers={"Authorization": f"Bearer {cget('render_key')}", "Accept": "application/json"},
                ) as r:
                    if r.status >= 300:
                        txt = (await r.text())[:150]
                        ST.sync_msg = f"Render API ত্রুটি ({r.status}) {txt}"
                        log.warning("render env %s -> %s %s", env, r.status, txt)
                        return False
                    os.environ[env] = v
        ST.sync_msg = f"Render-এ সেভ হয়েছে ({len(pairs)}টি মান) — Render একবার রিডিপ্লয় করতে পারে"
        return True
    except Exception as e:
        ST.sync_msg = f"Render-এ সংযোগ ব্যর্থ: {e}"
        log.warning("render sync failed: %s", e)
        return False


async def core_save(sync=True):
    ok_file = state_write()
    synced = await render_sync() if sync else False
    return {"file": ok_file, "render": synced}


def persistence():
    return {
        "file_ok": os.access(DATA_DIR, os.W_OK) if os.path.isdir(DATA_DIR) else os.access(BASE_DIR, os.W_OK),
        "render": render_ready(),
        "env_password": bool(os.getenv("ADMIN_PASSWORD")),
        "sync_msg": ST.sync_msg,
        "on_render": bool(os.getenv("RENDER")),
    }


# ------------------------------------------------------------------ crypto / secrets


def _fernet(seed):
    key = base64.urlsafe_b64encode(hashlib.sha256((seed + "|ay-file-host").encode()).digest())
    return Fernet(key)


def fernets():
    """এনক্রিপ্ট-ডিক্রিপ্ট কী: বট টোকেন থেকে। পুরোনো ভার্সনের (API_HASH) কী ফলব্যাক।"""
    out = [_fernet("tok:" + cget("token"))]
    legacy = cget("api_hash") or "5cf9676e75730d1e4c1ca4fafed515a5"
    out.append(_fernet(legacy))
    return out


def ensure_secrets():
    changed = False
    if not ST.cfg.get("link_secret"):
        ST.cfg["link_secret"] = secrets.token_hex(16)
        changed = True
    if not ST.cfg.get("api_key"):
        ST.cfg["api_key"] = secrets.token_urlsafe(24)
        changed = True
    return changed


def ready():
    return bool(ST.client and ST.entity and ST.cfg.get("link_secret"))


def base_url(request):
    u = cget("public_url") or cget("_auto_url")
    if u:
        return u.rstrip("/")
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{proto}://{request.host}"


def sign(mid):
    return hmac.new(ST.cfg["link_secret"].encode(), str(mid).encode(), "sha256").hexdigest()[:10]


def make_link(request, mid, name):
    return f"{base_url(request)}/f/{mid}-{sign(mid)}/{quote(name)}"


# ------------------------------------------------------------------ auth


def pw_hash(pw, salt=None, iters=150000):
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), iters).hex()}"


def pw_configured():
    return bool(ST.core.get("pw") or os.getenv("ADMIN_PASSWORD"))


def pw_check(pw):
    env = os.getenv("ADMIN_PASSWORD")
    if env and hmac.compare_digest(pw.encode(), env.encode()):
        return True
    stored = ST.core.get("pw")
    if stored and "$" in stored:
        salt, _ = stored.split("$", 1)
        # 150000 নতুন, 60000 পুরোনো ভার্সনের হ্যাশের জন্য
        return any(hmac.compare_digest(pw_hash(pw, salt, it), stored) for it in (150000, 60000))
    return False


def skey():
    return hashlib.sha256(
        (ST.core.get("sess_key", "") + "|" + (ST.core.get("pw") or "") + "|" + (os.getenv("ADMIN_PASSWORD") or "")).encode()
    ).digest()


def make_session():
    days = int(ST.cfg.get("session_days", 7))
    exp = str(int(time.time()) + days * 86400)
    return f"{exp}.{hmac.new(skey(), exp.encode(), 'sha256').hexdigest()}"


def authed(request):
    v = request.cookies.get("ay_sess", "")
    try:
        exp, sig = v.split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, hmac.new(skey(), exp.encode(), "sha256").hexdigest())
    except Exception:
        return False


def api_ok(request):
    k = request.headers.get("X-API-Key", "")
    if k and ST.cfg.get("api_key") and hmac.compare_digest(k, ST.cfg["api_key"]):
        return "key"
    return "session" if authed(request) else None


def set_cookie(resp, request):
    secure = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
    resp.set_cookie("ay_sess", make_session(), max_age=int(ST.cfg.get("session_days", 7)) * 86400,
                    httponly=True, samesite="Strict", secure=secure, path="/")


def client_ip(request):
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[-1].strip() if xff else (request.remote or "?")


def locked(ip):
    now = time.time()
    cnt, first = ST.fails.get(ip, (0, now))
    window = int(ST.cfg.get("lockout_minutes", 10)) * 60
    if now - first > window:
        ST.fails.pop(ip, None)
        return False
    return cnt >= int(ST.cfg.get("login_max_fails", 5))


def fail(ip):
    now = time.time()
    if len(ST.fails) > 2000:
        ST.fails.clear()
    cnt, first = ST.fails.get(ip, (0, now))
    ST.fails[ip] = (cnt + 1, first)


# ------------------------------------------------------------------ telegram


async def resolve_group(c, gid):
    gid = int(gid)
    if -1000000000000 < gid < 0:  # basic group
        return types.InputPeerChat(-gid)
    try:
        return await c.get_input_entity(gid)
    except ValueError:
        pass
    cid = int(str(gid)[4:]) if str(gid).startswith("-100") else abs(gid)
    r = await c(functions.channels.GetChannelsRequest([types.InputChannel(cid, 0)]))
    ch = r.chats[0]
    return types.InputPeerChannel(ch.id, ch.access_hash)


async def tg_disconnect():
    if ST.client:
        try:
            await ST.client.disconnect()
        except Exception:
            pass
    ST.client = None
    ST.entity = None
    ST.group_title = ""
    ST.meta.clear()


async def find_cfg_msg():
    ent = ST.entity
    pinned = None
    try:
        if isinstance(ent, types.InputPeerChannel):
            full = await ST.client(functions.channels.GetFullChannelRequest(ent))
            pinned = full.full_chat.pinned_msg_id
        elif isinstance(ent, types.InputPeerChat):
            full = await ST.client(functions.messages.GetFullChatRequest(ent.chat_id))
            pinned = full.full_chat.pinned_msg_id
    except Exception as e:
        log.warning("pinned lookup failed: %s", e)
    if pinned:
        m = await ST.client.get_messages(ent, ids=pinned)
        if m and m.message and m.message.startswith(MARK):
            return m
    return None


async def load_config():
    ST.cfg_mid = None
    resave = False
    m = await find_cfg_msg()
    if m:
        raw = None
        for i, f in enumerate(fernets()):
            try:
                raw = f.decrypt(m.message.split("\n", 1)[1].strip().encode())
                resave = resave or i > 0  # পুরোনো কী দিয়ে পড়া গেছে — নতুন কী দিয়ে আবার লিখব
                break
            except (InvalidToken, IndexError, ValueError):
                continue
        ST.cfg_mid = m.id
        if raw is not None:
            try:
                remote = json.loads(raw)
                ST.cfg = {**DEFAULTS, **{k: v for k, v in remote.items() if k in DEFAULTS}}
                # পুরোনো ভার্সনের পাসওয়ার্ড মাইগ্রেশন
                if remote.get("pw") and not ST.core.get("pw"):
                    ST.core["pw"] = remote["pw"]
                    await core_save()
            except ValueError:
                raw = None
        if raw is None:
            resave = True
            log.warning("config message unreadable — current/local settings will be re-saved")
    if ensure_secrets() or ST.cfg_mid is None or resave:
        await save_config()
    state_write()


async def save_config():
    if not ST.entity or not ST.client:
        return False
    text = MARK + "\n" + fernets()[0].encrypt(json.dumps(ST.cfg).encode()).decode()
    if ST.cfg_mid:
        try:
            await ST.client.edit_message(ST.entity, ST.cfg_mid, text)
            return True
        except errors.MessageNotModifiedError:
            return True
        except Exception as e:
            log.warning("config edit failed, creating new: %s", e)
    m = await ST.client.send_message(ST.entity, text)
    try:
        await ST.client.pin_message(ST.entity, m.id, notify=False)
    except Exception as e:
        log.warning("pin failed (give the bot 'Pin messages' right): %s", e)
    ST.cfg_mid = m.id
    return True


async def persist_cfg():
    state_write()
    return await save_config()


async def connect():
    async with ST.lock:
        await tg_disconnect()
        ST.error = ""
        try:
            api_id = int(cget("api_id", "0"))
        except ValueError:
            api_id = 0
        api_hash = cget("api_hash")
        if not (api_id and api_hash):
            ST.error = "API ID / API Hash সেট করা নেই — সেটিংস > টেলিগ্রাম থেকে দিন (my.telegram.org থেকে পাবেন)"
            return
        if not cget("token"):
            ST.error = "বট টোকেন সেট করা নেই — সেটিংস > টেলিগ্রাম থেকে দিন"
            return
        try:
            c = TelegramClient(StringSession(), api_id, api_hash, connection_retries=5, retry_delay=2,
                               flood_sleep_threshold=120)
            await c.start(bot_token=cget("token"))
            ST.client = c
            ST.bot = (await c.get_me()).username or ""
        except errors.FloodWaitError as e:
            ST.client = None
            ST.error = f"টেলিগ্রাম লগইনে সীমা: {e.seconds} সেকেন্ড পরে আবার চেষ্টা করুন"
            return
        except errors.ApiIdInvalidError:
            ST.client = None
            ST.error = "API ID / API Hash ভুল — my.telegram.org থেকে আবার দেখে নিন"
            return
        except Exception as e:
            ST.client = None
            ST.error = f"বট লগইন ব্যর্থ: {e}"
            return
        if not cget("group_id"):
            ST.error = "গ্রুপ আইডি সেট করা নেই — সেটিংস > টেলিগ্রাম থেকে দিন"
            return
        try:
            ST.entity = await resolve_group(ST.client, cget("group_id"))
            try:
                ent = await ST.client.get_entity(ST.entity)
                ST.group_title = getattr(ent, "title", "") or ""
            except Exception:
                ST.group_title = ""
            await load_config()
            ST.top = max(ST.top, ST.cfg_mid or 0)
            ST.sem = asyncio.Semaphore(int(ST.cfg.get("concurrency", 3)))
        except Exception as e:
            ST.entity = None
            ST.error = (f"গ্রুপ অ্যাক্সেস ব্যর্থ ({e}). বটকে গ্রুপে অ্যাডমিন করুন "
                        "(Post, Delete, Pin অনুমতিসহ) এবং গ্রুপ আইডি ঠিক আছে কিনা দেখুন")


async def boot():
    for _ in range(6):
        await connect()
        if ready() or not (cget("api_id") and cget("api_hash") and cget("token") and cget("group_id")):
            return
        await asyncio.sleep(15)


async def keepalive():
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        await asyncio.sleep(600)
        if not ST.cfg.get("keepalive", True):
            continue
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(url + "/healthz", timeout=aiohttp.ClientTimeout(total=15))
        except Exception:
            pass


async def diagnose():
    """কানেকশন চেকলিস্ট — প্যানেলের 'পরীক্ষা করুন' বাটনের জন্য।"""
    out = []

    def add(name, ok, detail=""):
        out.append({"name": name, "ok": ok, "detail": detail})

    add("API ID / Hash", bool(cget("api_id") and cget("api_hash")), "সেট আছে" if cget("api_id") and cget("api_hash") else "সেট নেই")
    add("বট টোকেন", bool(cget("token")), "সেট আছে" if cget("token") else "সেট নেই")
    add("বট লগইন", bool(ST.client), ("@" + ST.bot) if ST.client else (ST.error or "লগইন হয়নি"))
    add("গ্রুপ আইডি", bool(cget("group_id")), cget("group_id") or "সেট নেই")
    add("গ্রুপ অ্যাক্সেস", bool(ST.entity), ST.group_title or ("ঠিক আছে" if ST.entity else "অ্যাক্সেস নেই"))
    if ST.entity and isinstance(ST.entity, types.InputPeerChannel):
        try:
            r = await ST.client(functions.channels.GetParticipantRequest(ST.entity, types.InputPeerSelf()))
            p = r.participant
            if isinstance(p, types.ChannelParticipantCreator):
                add("বটের অ্যাডমিন অনুমতি", True, "ওনার")
            elif isinstance(p, types.ChannelParticipantAdmin):
                rt = p.admin_rights
                need = {"Post": rt.post_messages if rt.post_messages is not None else True,
                        "Delete": rt.delete_messages, "Pin": rt.pin_messages}
                missing = [k for k, v in need.items() if not v]
                add("বটের অ্যাডমিন অনুমতি", not missing, "Post/Delete/Pin সব আছে" if not missing else "নেই: " + ", ".join(missing))
            else:
                add("বটের অ্যাডমিন অনুমতি", False, "বট অ্যাডমিন নয়")
        except Exception as e:
            add("বটের অ্যাডমিন অনুমতি", False, str(e)[:120])
    elif ST.entity:
        add("গ্রুপের ধরন", True, "বেসিক গ্রুপ — সুপারগ্রুপে আপগ্রেড করলে আরও স্থিতিশীল")
    add("সেটিংস মেসেজ (পিন)", bool(ST.cfg_mid), "গ্রুপে পিন করা আছে" if ST.cfg_mid else "নেই (বটকে Pin অনুমতি দিন)")
    return out


# ------------------------------------------------------------------ file listing / meta


def is_file(m):
    return bool(m and m.media and getattr(m, "file", None) and m.id != ST.cfg_mid)


def msg_info(m):
    f = m.file
    mime = f.mime_type or "application/octet-stream"
    name = f.name or f"file_{m.id}"
    return {"msg": m, "size": f.size or 0, "mime": mime, "name": safe_name(name, mime)}


async def get_meta(mid):
    now = time.time()
    c = ST.meta.get(mid)
    if c and now - c[0] < 20:
        return c[1]
    m = await ST.client.get_messages(ST.entity, ids=mid)
    meta = msg_info(m) if is_file(m) else None
    if len(ST.meta) > 500:
        ST.meta.clear()
    ST.meta[mid] = (now, meta)
    return meta


async def find_top():
    top = max(ST.top, ST.cfg_mid or 0)
    cur, empty = top, 0
    while empty < 3:
        ms = await ST.client.get_messages(ST.entity, ids=list(range(cur + 1, cur + 101)))
        got = [m.id for m in ms if m]
        if got:
            top = max(got)
            cur, empty = top, 0
        else:
            cur += 100
            empty += 1
    ST.top = top
    return top


async def recent_files(limit, before=0, scan_cap=400):
    """নতুন থেকে পুরোনো ক্রমে ফাইল মেসেজ। রিটার্ন: (মেসেজ লিস্ট, পরের cursor (0 = শেষ))."""
    out = []
    try:
        scanned = 0
        async for m in ST.client.iter_messages(ST.entity, offset_id=before or 0):
            scanned += 1
            if is_file(m):
                out.append(m)
            if len(out) >= limit or scanned >= scan_cap:
                return out, m.id
        return out, 0
    except Exception as e:  # বট হিস্ট্রি পড়তে না পারলে আইডি ধরে স্ক্যান
        log.info("iter_messages unavailable (%s) — scanning ids", e)
    out, scanned = [], 0
    cur = (before - 1) if before else await find_top()
    while cur > 0 and len(out) < limit and scanned < scan_cap:
        ids = list(range(cur, max(cur - 100, 0), -1))
        for m in await ST.client.get_messages(ST.entity, ids=ids):
            if is_file(m):
                out.append(m)
        cur -= 100
        scanned += 100
    out.sort(key=lambda m: -m.id)
    if len(out) > limit:
        out = out[:limit]
        return out, out[-1].id
    return out, (cur + 1 if cur > 0 else 0)


def kind_of(mime):
    for k in ("image", "video", "audio"):
        if mime.startswith(k + "/"):
            return k
    return "file"


# ------------------------------------------------------------------ upload


class BodyReader:
    """রিকোয়েস্ট বডির ওপর async ফাইল-লাইক। ডিস্ক/বাফার ছাড়া স্ট্রিম।"""

    def __init__(self, content, size, name):
        self.content, self.left, self.name = content, size, name

    async def read(self, n=-1):
        n = self.left if n is None or n < 0 else min(n, self.left)
        if n <= 0:
            return b""
        try:
            data = await self.content.readexactly(n)
        except asyncio.IncompleteReadError as e:
            data = e.partial
        self.left -= len(data)
        return data


async def tg_send(handle, name, mime, size):
    media = types.InputMediaUploadedDocument(file=handle, mime_type=mime, force_file=True,
                                             attributes=[types.DocumentAttributeFilename(name)])
    caption = f"📁 {name}\n💾 {human(size)}" if ST.cfg.get("caption", True) else ""
    r = await ST.client(functions.messages.SendMediaRequest(
        peer=ST.entity, media=media, message=caption, random_id=random.randrange(-2 ** 63, 2 ** 63)))
    for u in getattr(r, "updates", []):
        if isinstance(u, (types.UpdateNewMessage, types.UpdateNewChannelMessage)) and getattr(u.message, "media", None):
            return u.message
    raise RuntimeError("টেলিগ্রাম থেকে মেসেজ আইডি পাওয়া যায়নি")


def blocked(name):
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    return ext in {e for e in ST.cfg.get("blocked_ext", "").split(",") if e}


async def h_upload(request):
    who = api_ok(request)
    if not who:
        return jr({"ok": False, "error": "অনুমতি নেই (লগইন করুন বা সঠিক X-API-Key দিন)"}, 401)
    if who == "key" and not ST.cfg.get("api_upload", True):
        return jr({"ok": False, "error": "API আপলোড বন্ধ আছে"}, 403)
    if not ready():
        return jr({"ok": False, "error": ST.error or "সার্ভার প্রস্তুত নয়"}, 503)
    size = request.content_length
    if not size:
        return jr({"ok": False, "error": "Content-Length লাগবে (ফাইল খালি বা অজানা সাইজ)"}, 411)
    if size > int(ST.cfg["max_size_mb"]) * 1024 * 1024:
        return jr({"ok": False, "error": f"সর্বোচ্চ {ST.cfg['max_size_mb']} MB"}, 413)
    raw = request.headers.get("X-Filename") or request.query.get("name") or "file"
    name0 = unquote(raw)
    ctype = request.headers.get("X-Mime") or request.content_type or ""
    mime = (mimetypes.guess_type(name0)[0]
            or (ctype if ctype not in ("", "application/x-www-form-urlencoded") else "")
            or "application/octet-stream")
    name = safe_name(name0, mime)
    if blocked(name):
        return jr({"ok": False, "error": "এই ধরনের ফাইল আপলোড নিষিদ্ধ"}, 415)
    reader = BodyReader(request.content, size, name)
    try:
        async with ST.sem:
            handle = await ST.client.upload_file(reader, file_size=size, file_name=name, part_size_kb=512)
            msg = await tg_send(handle, name, mime, size)
    except errors.FloodWaitError as e:
        return jr({"ok": False, "error": f"টেলিগ্রাম লিমিট: {e.seconds} সেকেন্ড পর চেষ্টা করুন"}, 429)
    except Exception as e:
        log.exception("upload failed")
        return jr({"ok": False, "error": f"আপলোড ব্যর্থ: {e}"}, 502)
    ST.top = max(ST.top, msg.id)
    ST.stats = (0, None)
    ST.counters["uploads"] += 1
    ST.counters["upload_bytes"] += size
    return jr({"ok": True, "id": msg.id, "name": name, "size": size, "mime": mime,
               "link": make_link(request, msg.id, name)})


# ------------------------------------------------------------------ serving files


def parse_range(h, size):
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", h.strip())
    if not m or (m[1] == "" and m[2] == ""):
        return None
    if m[1] == "":
        start, end = max(size - int(m[2]), 0), size - 1
    else:
        start, end = int(m[1]), min(int(m[2]) if m[2] else size - 1, size - 1)
    if start > end or start >= size:
        raise ValueError
    return start, end


def not_found(msg="ফাইল পাওয়া যায়নি (ডিলিট হয়েছে বা লিংক ভুল)", status=404):
    acc = html.escape(ST.cfg.get("accent", "#4f8cff"))
    return web.Response(status=status, content_type="text/html", text=
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{status}</title><body style='font-family:system-ui,sans-serif;background:#0d0f14;color:#e8eaf0;display:grid;place-items:center;height:100vh;margin:0'>"
        f"<div style='text-align:center;padding:20px'><h1 style='font-size:72px;margin:0;color:{acc}'>{status}</h1>"
        f"<p style='color:#8b93a5'>{html.escape(msg)}</p></div>")


async def h_file(request):
    if not ready():
        return not_found("সার্ভার প্রস্তুত নয়", 503)
    m = re.fullmatch(r"(\d+)-([0-9a-f]{10})", request.match_info["tok"])
    if not m or not hmac.compare_digest(sign(int(m[1])), m[2]):
        return not_found()
    try:
        meta = await get_meta(int(m[1]))
    except Exception as e:
        log.warning("meta error: %s", e)
        return not_found("টেলিগ্রাম থেকে তথ্য আনা যায়নি", 502)
    if not meta:
        return not_found()

    size, mime, name = meta["size"], meta["mime"], meta["name"]
    if mime == "application/octet-stream":
        mime = mimetypes.guess_type(name)[0] or mime
    start, end, status = 0, max(size - 1, 0), 200
    rng = request.headers.get("Range")
    if rng and size:
        try:
            r = parse_range(rng, size)
            if r:
                start, end = r
                status = 206
        except ValueError:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})

    inline = mime.startswith(("image/", "video/", "audio/", "text/")) or mime in ("application/pdf", "application/json")
    if request.query.get("dl"):
        inline = False
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name).replace('"', "_")
    ttl = int(ST.cfg.get("cache_seconds", 300))
    headers = {
        "Content-Type": mime + ("; charset=utf-8" if mime.startswith("text/") else ""),
        "Content-Length": str(end - start + 1),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        "Cache-Control": f"public, max-age={ttl}" if ttl else "no-store",
        "Access-Control-Allow-Origin": "*",
        "X-Content-Type-Options": "nosniff",
    }
    if mime in ("text/html", "image/svg+xml", "application/xhtml+xml") and ST.cfg.get("html_sandbox", True):
        headers["Content-Security-Policy"] = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(request)
    if request.method == "HEAD" or not size:
        return resp
    ST.counters["downloads"] += 1
    aligned = start - (start % CHUNK)
    skip, remaining = start - aligned, end - start + 1
    try:
        async for chunk in ST.client.iter_download(meta["msg"].media, offset=aligned, request_size=CHUNK, file_size=size):
            if skip:
                chunk, skip = chunk[skip:], 0
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            await resp.write(chunk)
            remaining -= len(chunk)
            ST.counters["download_bytes"] += len(chunk)
            if remaining <= 0:
                break
        await resp.write_eof()
    except (ConnectionResetError, asyncio.CancelledError, aiohttp.ClientConnectionError):
        pass
    except Exception as e:
        log.warning("stream error: %s", e)
    return resp


# ------------------------------------------------------------------ pages & admin API


def render_page(name, **extra):
    t = PAGES[name]
    vals = {"SITE": html.escape(ST.cfg.get("site_name", "Ay File Host")),
            "ACCENT": html.escape(ST.cfg.get("accent", "#4f8cff")), **extra}
    for k, v in vals.items():
        t = t.replace("{{" + k + "}}", v)
    return t


async def h_index(request):
    if not pw_configured():
        page = render_page("login.html", MODE="setup")
    elif authed(request):
        page = render_page("panel.html")
    else:
        page = render_page("login.html", MODE="login")
    return web.Response(text=page, content_type="text/html",
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


async def h_health(request):
    return web.Response(text="ok")


async def h_login(request):
    ip = client_ip(request)
    if locked(ip):
        return jr({"ok": False, "error": f"অনেকবার ভুল — {ST.cfg.get('lockout_minutes', 10)} মিনিট পর চেষ্টা করুন"}, 429)
    try:
        d = await request.json()
    except Exception:
        d = {}
    ok = await asyncio.to_thread(pw_check, str(d.get("password", "")))
    if ok:
        ST.fails.pop(ip, None)
        resp = jr({"ok": True})
        set_cookie(resp, request)
        return resp
    fail(ip)
    await asyncio.sleep(1)
    return jr({"ok": False, "error": "পাসওয়ার্ড ভুল"}, 401)


async def h_setup(request):
    """প্রথমবার: অ্যাডমিন পাসওয়ার্ড তৈরি (শুধু যখন কোনো পাসওয়ার্ড সেট নেই)।"""
    if pw_configured():
        return jr({"ok": False, "error": "পাসওয়ার্ড আগেই সেট করা আছে"}, 403)
    d = await request.json()
    pw = str(d.get("password", ""))
    if len(pw) < 6:
        return jr({"ok": False, "error": "পাসওয়ার্ড কমপক্ষে ৬ অক্ষর"}, 400)
    ST.core["pw"] = await asyncio.to_thread(pw_hash, pw)
    await core_save()
    resp = jr({"ok": True})
    set_cookie(resp, request)
    return resp


async def h_logout(request):
    resp = jr({"ok": True})
    resp.del_cookie("ay_sess", path="/")
    return resp


def need_auth(fn):
    async def w(request):
        if not authed(request):
            return jr({"ok": False, "error": "লগইন করুন"}, 401)
        return await fn(request)
    return w


def need_api(fn):
    async def w(request):
        if not api_ok(request):
            return jr({"ok": False, "error": "অনুমতি নেই"}, 401)
        return await fn(request)
    return w


@web.middleware
async def guard(request, handler):
    """CSRF: ক্রস-সাইট থেকে আসা সেশন-ভিত্তিক পরিবর্তন আটকানো।"""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.path.startswith("/api/"):
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host and not request.headers.get("X-API-Key"):
            return jr({"ok": False, "error": "অননুমোদিত অরিজিন"}, 403)
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except json.JSONDecodeError:
        return jr({"ok": False, "error": "ভুল JSON"}, 400)
    except Exception as e:
        log.exception("handler error")
        return jr({"ok": False, "error": f"সার্ভার ত্রুটি: {e}"}, 500)


@need_auth
async def h_status(request):
    api_id = cget("api_id")
    return jr({
        "ok": True, "version": VERSION, "connected": ready(), "bot": ST.bot, "error": ST.error,
        "group_title": ST.group_title, "group_id": cget("group_id"),
        "api_id": api_id, "api_hash": mask(cget("api_hash")), "has_api": bool(api_id and cget("api_hash")),
        "token": mask(cget("token"), 6),
        "cfg": {k: ST.cfg.get(k, v) for k, v in DEFAULTS.items() if k in SCHEMA},
        "api_key": ST.cfg.get("api_key", ""),
        "public_url": cget("public_url"), "auto_url": cget("_auto_url"), "base_url": base_url(request),
        "render_key": mask(cget("render_key")), "render_service": cget("render_service"),
        "persist": persistence(),
        "counters": ST.counters, "uptime": int(time.time() - STARTED),
        "python": sys.version.split()[0],
    })


@need_api
async def h_files(request):
    if not ready():
        return jr({"ok": False, "error": ST.error or "প্রস্তুত নয়"}, 503)
    limit = max(1, min(int(request.query.get("limit", 30)), 100))
    before = int(request.query.get("before", 0) or 0)
    try:
        msgs, nxt = await recent_files(limit, before)
    except Exception as e:
        return jr({"ok": False, "error": str(e)}, 502)
    items = []
    for m in msgs:
        i = msg_info(m)
        items.append({"id": m.id, "name": i["name"], "size": i["size"], "mime": i["mime"],
                      "kind": kind_of(i["mime"]), "date": m.date.isoformat() if m.date else "",
                      "link": make_link(request, m.id, i["name"])})
    return jr({"ok": True, "items": items, "next": nxt})


@need_auth
async def h_stats(request):
    if not ready():
        return jr({"ok": False}, 503)
    ts, val = ST.stats
    if val is None or time.time() - ts > 60:
        msgs, nxt = await recent_files(100000, 0, scan_cap=1500)
        val = {"count": len(msgs), "size": sum(msg_info(m)["size"] for m in msgs), "partial": bool(nxt)}
        ST.stats = (time.time(), val)
    return jr({"ok": True, **val, "size_h": human(val["size"])})


@need_api
async def h_delete(request):
    if not ready():
        return jr({"ok": False, "error": "প্রস্তুত নয়"}, 503)
    mid = int(request.match_info["mid"])
    if mid == ST.cfg_mid:
        return jr({"ok": False, "error": "কনফিগ মেসেজ ডিলিট করা যাবে না"}, 400)
    try:
        await ST.client.delete_messages(ST.entity, [mid])
    except Exception as e:
        return jr({"ok": False, "error": f"ডিলিট ব্যর্থ (বটের Delete অনুমতি আছে?): {e}"}, 502)
    ST.meta.pop(mid, None)
    ST.stats = (0, None)
    return jr({"ok": True})


def validate(d):
    """SCHEMA অনুযায়ী ইনপুট যাচাই। রিটার্ন (পরিষ্কার মান, ত্রুটি)।"""
    clean, errs = {}, {}
    for k, v in d.items():
        spec = SCHEMA.get(k)
        if not spec:
            continue
        t = spec[0]
        try:
            if t == "str":
                s = str(v).strip()
                if not (spec[1] <= len(s) <= spec[2]):
                    raise ValueError(f"{spec[1]}–{spec[2]} অক্ষর হতে হবে")
                clean[k] = s
            elif t == "int":
                n = int(v)
                if not (spec[1] <= n <= spec[2]):
                    raise ValueError(f"{spec[1]} থেকে {spec[2]}-এর মধ্যে হতে হবে")
                clean[k] = n
            elif t == "bool":
                clean[k] = bool(v)
            elif t == "color":
                s = str(v).strip()
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", s):
                    raise ValueError("রঙ #RRGGBB ফরম্যাটে দিন")
                clean[k] = s.lower()
            elif t == "exts":
                parts = [p.strip().lower().lstrip(".") for p in re.split(r"[,\s]+", str(v)) if p.strip()]
                if any(not re.fullmatch(r"[a-z0-9]{1,10}", p) for p in parts):
                    raise ValueError("শুধু এক্সটেনশন দিন, কমা দিয়ে (যেমন exe,bat)")
                clean[k] = ",".join(sorted(set(parts)))
        except (ValueError, TypeError) as e:
            errs[k] = str(e) if isinstance(e, ValueError) and str(e) and not str(e).startswith("invalid literal") else "ভুল মান"
    return clean, errs


@need_auth
async def h_set_general(request):
    d = await request.json()
    clean, errs = validate(d)
    if errs:
        return jr({"ok": False, "error": "; ".join(f"{k}: {v}" for k, v in errs.items()), "fields": errs}, 400)
    if d.get("regen_api_key"):
        clean["api_key"] = secrets.token_urlsafe(24)
    ST.cfg.update(clean)
    if "concurrency" in clean:
        ST.sem = asyncio.Semaphore(clean["concurrency"])
    saved = await persist_cfg()
    return jr({"ok": True, "saved_to_group": bool(saved)})


@need_auth
async def h_set_password(request):
    d = await request.json()
    if not await asyncio.to_thread(pw_check, str(d.get("current", ""))):
        return jr({"ok": False, "error": "বর্তমান পাসওয়ার্ড ভুল"}, 403)
    new = str(d.get("new", ""))
    if len(new) < 6:
        return jr({"ok": False, "error": "নতুন পাসওয়ার্ড কমপক্ষে ৬ অক্ষর"}, 400)
    ST.core["pw"] = await asyncio.to_thread(pw_hash, new)
    res = await core_save()
    resp = jr({"ok": True, **res})
    set_cookie(resp, request)
    return resp


@need_auth
async def h_regen_secret(request):
    if not ready():
        return jr({"ok": False, "error": "আগে কানেক্ট করুন"}, 503)
    ST.cfg["link_secret"] = secrets.token_hex(16)
    ST.meta.clear()
    await persist_cfg()
    return jr({"ok": True})


@need_auth
async def h_logout_all(request):
    ST.core["sess_key"] = secrets.token_hex(24)
    state_write()
    resp = jr({"ok": True})
    resp.del_cookie("ay_sess", path="/")
    return resp


@need_auth
async def h_set_telegram(request):
    """API ID, API Hash, Bot Token, Group ID — যেকোনোটি (খালি = আগেরটাই থাকবে)।"""
    d = await request.json()
    new = {}
    api_id = str(d.get("api_id", "")).strip()
    api_hash = str(d.get("api_hash", "")).strip()
    token = str(d.get("token", "")).strip()
    gid = str(d.get("group_id", "")).strip()
    if api_id:
        if not re.fullmatch(r"\d{3,12}", api_id):
            return jr({"ok": False, "error": "API ID শুধু সংখ্যা (যেমন 1234567)"}, 400)
        new["api_id"] = api_id
    if api_hash:
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            return jr({"ok": False, "error": "API Hash ৩২ অক্ষরের হেক্স কোড (my.telegram.org থেকে কপি করুন)"}, 400)
        new["api_hash"] = api_hash.lower()
    if token:
        if not re.fullmatch(r"\d{5,}:[\w-]{30,}", token):
            return jr({"ok": False, "error": "বট টোকেনের ফরম্যাট ভুল (123456:ABC… এমন হবে)"}, 400)
        new["token"] = token
    if gid:
        if not re.fullmatch(r"-?\d+", gid):
            return jr({"ok": False, "error": "গ্রুপ আইডি শুধু সংখ্যা (যেমন -1001234567890)"}, 400)
        if not gid.startswith("-") and len(gid) >= 9:
            gid = "-100" + gid  # সুপারগ্রুপের আইডি মাইনাস ছাড়া দিলে ঠিক করে নেয়
        new["group_id"] = gid
    if not new:
        return jr({"ok": False, "error": "কিছু পরিবর্তন করা হয়নি"}, 400)

    old_core, old_cfg = dict(ST.core), dict(ST.cfg)
    if "group_id" in new and new["group_id"] != cget("group_id"):
        ST.cfg["link_secret"] = ""  # মেসেজ আইডি গ্রুপভেদে আলাদা
        ST.cfg_mid = None
    ST.core.update(new)
    await connect()
    if not ready():
        err = ST.error
        ST.core, ST.cfg = old_core, old_cfg
        await connect()
        return jr({"ok": False, "error": err or "কানেক্ট করা যায়নি"}, 400)
    if "token" in new:
        await save_config()  # নতুন কী দিয়ে সেটিংস মেসেজ আবার এনক্রিপ্ট
    res = await core_save()
    resp = jr({"ok": True, "bot": ST.bot, "group": ST.group_title, **res, "sync_msg": ST.sync_msg})
    set_cookie(resp, request)
    return resp


@need_auth
async def h_set_server(request):
    """পাবলিক URL ও Render ইন্টিগ্রেশন।"""
    d = await request.json()
    if "public_url" in d:
        u = str(d["public_url"]).strip().rstrip("/")
        if u and not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", u):
            return jr({"ok": False, "error": "URL ঠিক নয় (https://example.com)"}, 400)
        ST.core["public_url"] = u
        if not u:
            ST.core.pop("public_url", None)
    if str(d.get("render_key", "")).strip():
        ST.core["render_key"] = str(d["render_key"]).strip()
    if "render_service" in d and str(d["render_service"]).strip():
        sid = str(d["render_service"]).strip()
        if not re.fullmatch(r"srv-[\w]+", sid):
            return jr({"ok": False, "error": "Render Service ID srv-… দিয়ে শুরু হয়"}, 400)
        ST.core["render_service"] = sid
    if not ST.core.get("render_service") and os.getenv("RENDER_SERVICE_ID"):
        ST.core["render_service"] = os.getenv("RENDER_SERVICE_ID")
    ST.meta.clear()
    res = await core_save()
    return jr({"ok": True, **res, "sync_msg": ST.sync_msg})


@need_auth
async def h_render_sync(request):
    ok = await render_sync(force=True)
    return jr({"ok": ok, "sync_msg": ST.sync_msg}, 200 if ok else 400)


@need_auth
async def h_reconnect(request):
    await connect()
    return jr({"ok": ready(), "error": ST.error})


@need_auth
async def h_diagnose(request):
    return jr({"ok": True, "checks": await diagnose()})


@need_auth
async def h_logs(request):
    return jr({"ok": True, "logs": list(LOGS)[-150:]})


@need_auth
async def h_restart(request):
    log.info("restart requested from panel")
    asyncio.get_running_loop().call_later(0.7, os._exit, 0)
    return jr({"ok": True})


# ------------------------------------------------------------------ app


async def on_startup(app):
    ST.lock = asyncio.Lock()
    ST.sem = asyncio.Semaphore(3)
    core_load()
    ST.sem = asyncio.Semaphore(int(ST.cfg.get("concurrency", 3)))
    if not ST.core.get("render_service") and os.getenv("RENDER_SERVICE_ID"):
        ST.core["render_service"] = os.getenv("RENDER_SERVICE_ID")
    for n in ("login.html", "panel.html"):
        with open(os.path.join(BASE_DIR, "static", n), encoding="utf-8") as f:
            PAGES[n] = f.read()
    app["tasks"] = [asyncio.create_task(boot()), asyncio.create_task(keepalive())]


async def on_cleanup(app):
    for t in app["tasks"]:
        t.cancel()
    await tg_disconnect()


def build_app():
    app = web.Application(client_max_size=1024 ** 2, middlewares=[guard])
    r = app.router
    r.add_get("/", h_index)
    r.add_get("/healthz", h_health)
    r.add_route("GET", "/f/{tok}/{name:.*}", h_file)
    r.add_route("HEAD", "/f/{tok}/{name:.*}", h_file)
    r.add_post("/api/login", h_login)
    r.add_post("/api/setup", h_setup)
    r.add_post("/api/logout", h_logout)
    r.add_post("/api/logout-all", h_logout_all)
    r.add_get("/api/status", h_status)
    r.add_get("/api/files", h_files)
    r.add_get("/api/stats", h_stats)
    r.add_get("/api/logs", h_logs)
    r.add_get("/api/diagnose", h_diagnose)
    r.add_delete("/api/files/{mid:\\d+}", h_delete)
    r.add_route("POST", "/api/upload", h_upload)
    r.add_route("PUT", "/api/upload", h_upload)
    r.add_post("/api/settings/general", h_set_general)
    r.add_post("/api/settings/password", h_set_password)
    r.add_post("/api/settings/telegram", h_set_telegram)
    r.add_post("/api/settings/server", h_set_server)
    r.add_post("/api/settings/regen-link-secret", h_regen_secret)
    r.add_post("/api/render-sync", h_render_sync)
    r.add_post("/api/reconnect", h_reconnect)
    r.add_post("/api/restart", h_restart)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=PORT, access_log=None)
