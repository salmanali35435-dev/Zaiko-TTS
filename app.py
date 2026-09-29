import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import requests
import streamlit as st
import extra_streamlit_components as stx

from database import (
    authenticate_user,
    create_session,
    create_user,
    delete_session,
    delete_voice,
    get_credentials,
    get_deployment,
    get_user_by_session,
    get_voice,
    init_db,
    list_voices,
    save_credentials,
    save_voice,
    upsert_deployment,
    admin_list_users,
    admin_update_user,
    admin_create_client,
    admin_revoke_user,
    admin_grant_access,
    admin_stats,
)


APP_NAME = "ZAIKO AI STUDIO"

SESSION_DAYS = int(
    os.getenv("SESSION_DAYS", "30")
)

POLL_SECONDS = int(
    os.getenv("POLL_SECONDS", "5")
)

SIGNUP_CODE = os.getenv(
    "SIGNUP_CODE",
    "",
).strip()

# ============================================================
# F5-TTS SESSION SETTINGS
# ============================================================

F5_SESSION_MINUTES = 20
F5_SESSION_SECONDS = F5_SESSION_MINUTES * 60

READY_MARKER = "__ZAIKO_READY_AT__:"

ACTIVE_DEPLOYMENT_STATES = {
    "SUBMITTING",
    "QUEUED",
    "RUNNING",
    "READY",
}

st.set_page_config(
    page_title=APP_NAME,
    page_icon="🎙️",
    layout="wide",
)


@st.cache_resource
def boot():
    init_db()
    return True


boot()

cookies = stx.CookieManager(
    key="f5tts-cookie"
)


# ============================================================
# HELPERS
# ============================================================

def normalize_domain(v):
    v = v.strip()

    if not v:
        return ""

    if not v.startswith(
        ("http://", "https://")
    ):
        v = "https://" + v

    p = urlparse(v)

    return (
        p.netloc or p.path.split("/")[0]
    ).lower().rstrip("/")


def cookie_get():
    try:
        value = cookies.get(
            "f5tts_session"
        )

        if value:
            return value

        try:
            all_cookies = cookies.get_all()

            if isinstance(
                all_cookies,
                dict,
            ):
                return all_cookies.get(
                    "f5tts_session"
                )

        except Exception:
            pass

    except Exception:
        pass

    return None


def cookie_set(v):
    try:
        if not v:
            return False

        cookies.set(
            "f5tts_session",
            v,
            expires_at=(
                time.time()
                + SESSION_DAYS * 86400
            ),
        )

        return True

    except Exception:
        return False


# ============================================================
# PAGE PERSISTENCE
# ============================================================

def page_cookie_get(name, default):
    try:
        value = cookies.get(name)

        allowed = (
            "Dashboard",
            "Settings",
            "Active Clients",
            "Revoked Clients",
            "Create New Client",
        )

        if value in allowed:
            return value

        try:
            all_cookies = cookies.get_all()

            if isinstance(
                all_cookies,
                dict,
            ):
                value = all_cookies.get(name)

                if value in allowed:
                    return value

        except Exception:
            pass

    except Exception:
        pass

    return default


def page_cookie_set(name, value):
    try:
        if not value:
            return

        cookies.set(
            name,
            value,
            expires_at=(
                time.time()
                + SESSION_DAYS * 86400
            ),
        )

    except Exception:
        pass


def logout():
    st.session_state.pop(
        "client_authenticated_user_id",
        None,
    )

    st.session_state.pop(
        "client_authenticated_user",
        None,
    )

    raw_cookie = cookie_get()

    if raw_cookie:
        delete_session(raw_cookie)

    try:
        cookies.delete(
            "f5tts_session"
        )

        cookies.delete(
            "f5tts_client_page"
        )

    except Exception:
        pass

    st.rerun()


# ============================================================
# KAGGLE COMMAND
# ============================================================

def run_kaggle(
    args,
    user,
    token,
    timeout=20,
):
    env = os.environ.copy()

    env["KAGGLE_USERNAME"] = user
    env["KAGGLE_API_TOKEN"] = token

    try:
        p = subprocess.run(
            ["kaggle", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        return p.returncode, (
            p.stdout or ""
        ) + (
            p.stderr or ""
        )

    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""

        if isinstance(stdout, bytes):
            stdout = stdout.decode(
                "utf-8",
                errors="replace",
            )

        if isinstance(stderr, bytes):
            stderr = stderr.decode(
                "utf-8",
                errors="replace",
            )

        return (
            124,
            (
                f"[KAGGLE TIMEOUT] "
                f"{' '.join(args)}\n"
                f"{stdout}\n"
                f"{stderr}"
            ).strip(),
        )

    except Exception as exc:
        return (
            1,
            f"[KAGGLE COMMAND ERROR] {exc}",
        )


# ============================================================
# KAGGLE STATUS
# ============================================================

def status(
    kernel,
    user,
    token,
):
    code, out = run_kaggle(
        [
            "kernels",
            "status",
            kernel,
        ],
        user,
        token,
        12,
    )

    low = out.lower()

    if code == 124:
        return "UNKNOWN", out

    if code:
        return (
            "NOT_FOUND"
            if any(
                x in low
                for x in (
                    "not found",
                    "404",
                    "does not exist",
                    "could not find",
                )
            )
            else "ERROR"
        ), out

    if "running" in low:
        return "RUNNING", out

    if (
        "queued" in low
        or "pending" in low
    ):
        return "QUEUED", out

    if (
        "complete" in low
        or "success" in low
    ):
        return "COMPLETE", out

    if (
        "error" in low
        or "failed" in low
    ):
        return "ERROR", out

    return "UNKNOWN", out


# ============================================================
# LIVE KAGGLE LOG MONITOR
#
# IMPORTANT:
# Logs are NEVER displayed to user.
# They are only internally checked for PUBLIC_URL.
# ============================================================

def logs(
    kernel,
    user,
    token,
):
    env = os.environ.copy()

    env["KAGGLE_USERNAME"] = user
    env["KAGGLE_API_TOKEN"] = token

    try:
        p = subprocess.Popen(
            [
                "kaggle",
                "kernels",
                "logs",
                kernel,
                "--follow",
                "--interval",
                "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )

    except Exception as exc:
        return (
            f"[LOG FETCH ERROR] {exc}"
        )

    collected = []
    started = time.time()

    try:
        while (
            time.time() - started
            < 20
        ):
            if p.stdout is None:
                break

            line = p.stdout.readline()

            if line:
                collected.append(line)

                current = "".join(
                    collected
                )

                if re.search(
                    r"PUBLIC_URL\s*:\s*https?://[^\s]+",
                    current,
                    flags=re.IGNORECASE,
                ):
                    break

            else:
                if p.poll() is not None:
                    break

                time.sleep(0.1)

    except Exception as exc:
        collected.append(
            f"\n[LOG FETCH ERROR] {exc}\n"
        )

    finally:
        try:
            p.terminate()
        except Exception:
            pass

        try:
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    return "".join(
        collected
    )[-30000:]


# ============================================================
# KAGGLE INTERNAL API
# Used only for finding/canceling active session.
# ============================================================

def _kaggle_internal_post(
    token,
    method,
    payload,
):
    try:
        session = requests.Session()

        session.headers.update(
            {
                "Authorization": (
                    f"Bearer {token}"
                ),
                "Content-Type": (
                    "application/json"
                ),
                "User-Agent": (
                    "ZAIKO-AI-STUDIO"
                ),
            }
        )

        try:
            session.get(
                "https://www.kaggle.com/",
                timeout=10,
            )
        except Exception:
            pass

        xsrf = (
            session.cookies.get(
                "XSRF-TOKEN"
            )
            or session.cookies.get(
                "CSRF-TOKEN"
            )
        )

        headers = {}

        if xsrf:
            headers[
                "X-XSRF-TOKEN"
            ] = xsrf

        response = session.post(
            (
                "https://www.kaggle.com/"
                f"api/i/{method}"
            ),
            json=payload,
            headers=headers,
            timeout=20,
        )

        return (
            response.status_code,
            response.text or "",
        )

    except Exception as exc:
        return (
            0,
            str(exc),
        )


def get_kaggle_session_id(
    username,
    token,
    kernel_slug,
):
    code, body = (
        _kaggle_internal_post(
            token,
            (
                "kernels."
                "LegacyKernelsService/"
                "GetKernelViewModel"
            ),
            {
                "authorUserName": username,
                "kernelSlug": kernel_slug,
                "tab": "output",
            },
        )
    )

    if code != 200:
        return None

    try:
        data = json.loads(body)
    except Exception:
        return None

    kernel = (
        data.get("kernel")
        or {}
    )

    kernel_id = kernel.get("id")

    if not kernel_id:
        return None

    code, body = (
        _kaggle_internal_post(
            token,
            (
                "kernels."
                "KernelsService/"
                "ListKernelVersions"
            ),
            {
                "kernelId": kernel_id,
                "sortOption": "VERSION_ID",
                "pageSize": 30,
            },
        )
    )

    if code != 200:
        return None

    try:
        data = json.loads(body)
    except Exception:
        return None

    items = (
        data.get("items")
        or []
    )

    # Prefer an actually active run.
    for item in items:
        run = item.get("run") or {}

        session_id = run.get("id")
        run_status = str(
            run.get("status", "")
        ).lower()

        if (
            session_id
            and any(
                x in run_status
                for x in (
                    "running",
                    "queued",
                    "pending",
                )
            )
        ):
            return str(session_id)

    # Fallback to newest version/run.
    for item in items:
        run = item.get("run") or {}
        session_id = run.get("id")

        if session_id:
            return str(session_id)

    return None


def stop_kaggle_session(
    username,
    token,
    kernel_slug,
):
    session_id = (
        get_kaggle_session_id(
            username,
            token,
            kernel_slug,
        )
    )

    if not session_id:
        return (
            False,
            "Active Kaggle session ID could not be found.",
        )

    # --------------------------------------------------------
    # First: try CLI command if installed/supported.
    # --------------------------------------------------------

    code, out = run_kaggle(
        [
            "kernels",
            "cancel",
            "--session-id",
            session_id,
        ],
        username,
        token,
        30,
    )

    if code == 0:
        return True, out

    # --------------------------------------------------------
    # Fallback: Kaggle internal API.
    # --------------------------------------------------------

    status_code, body = (
        _kaggle_internal_post(
            token,
            (
                "kernels."
                "KernelsService/"
                "CancelKernelSession"
            ),
            {
                "sessionId": session_id
            },
        )
    )

    if status_code in (
        200,
        201,
        202,
        204,
    ):
        return True, body

    return (
        False,
        body
        or out
        or "Unable to cancel Kaggle session.",
    )


# ============================================================
# READY TIMER MARKER
# Stored in existing last_logs field.
# User never sees it.
# ============================================================

def make_ready_marker(timestamp):
    return (
        f"{READY_MARKER}"
        f"{timestamp:.6f}"
    )


def extract_ready_timestamp(dep):
    if not dep:
        return None

    text = getattr(
        dep,
        "last_logs",
        None,
    ) or ""

    match = re.search(
        re.escape(READY_MARKER)
        + r"\s*([0-9]+(?:\.[0-9]+)?)",
        text,
    )

    if not match:
        return None

    try:
        return float(
            match.group(1)
        )
    except Exception:
        return None


def deployment_seconds_left(dep):
    ready_at = (
        extract_ready_timestamp(dep)
    )

    if ready_at is None:
        return None

    return max(
        0,
        int(
            F5_SESSION_SECONDS
            - (
                time.time()
                - ready_at
            )
        ),
    )


# ============================================================
# FIND KERNEL
# ============================================================

def find_kernel(
    user,
    token,
    slug,
):
    code, out = run_kaggle(
        [
            "kernels",
            "list",
            "--mine",
            "--page-size",
            "100",
        ],
        user,
        token,
        60,
    )

    if code:
        return None

    for line in out.splitlines():
        if slug.lower() in line.lower():
            m = re.search(
                rf"{re.escape(user)}/([A-Za-z0-9_-]+)",
                line,
            )

            if m:
                return (
                    f"{user}/{m.group(1)}"
                )

    return None


# ============================================================
# BUILD KAGGLE KERNEL
# ============================================================

def build_kernel(
    folder,
    token,
    domain,
    voices,
):
    bundle = []

    for row in voices:
        v = get_voice(
            st.session_state.user_id,
            row.id,
        )

        if v:
            bundle.append(
                {
                    "name": v["name"],
                    "filename": v["filename"],
                    "data": base64.b64encode(
                        v["audio_bytes"]
                    ).decode(),
                }
            )

    code_lines = [
        'import base64, socket, subprocess, threading, time',
        'from pathlib import Path',
        'import requests',

        'print("[BOOT] Installing F5-TTS and ngrok")',

        'subprocess.run(["pip","install","-q","f5-tts","pyngrok"],check=True)',

        'from pyngrok import ngrok',

        'PORT=7860',

        'NGROK_AUTH_TOKEN=__TOKEN__',

        'NGROK_DOMAIN=__DOMAIN__',

        'VOICE_BUNDLE=__VOICES__',

        'voice_dir=Path("saved_voices"); voice_dir.mkdir(exist_ok=True)',

        'for item in VOICE_BUNDLE:',

        '    try: (voice_dir/item["filename"]).write_bytes(base64.b64decode(item["data"])); print("[VOICE] Restored:",item["name"])',

        '    except Exception as e: print("[VOICE] Restore failed:",e)',

        'print("[BOOT] Kaggle kernel started"); subprocess.run(["nvidia-smi"],check=False)',

        'log=Path("f5tts.log"); handle=open(log,"a",buffering=1)',

        'proc=subprocess.Popen(["f5-tts_infer-gradio","--host","0.0.0.0","--port",str(PORT)],stdout=handle,stderr=subprocess.STDOUT,text=True)',

        'def relay():',

        '    pos=0',

        '    while proc.poll() is None:',

        '        try:',

        '            if log.exists():',

        '                with open(log,"r",encoding="utf-8",errors="replace") as f: f.seek(pos); chunk=f.read(); pos=f.tell()',

        '                for line in chunk.splitlines(): print("[F5]",line)',

        '        except Exception: pass',

        '        time.sleep(2)',

        'threading.Thread(target=relay,daemon=True).start()',

        'deadline=time.time()+900',

        'while time.time()<deadline:',

        '    if proc.poll() is not None: raise RuntimeError("F5-TTS exited with code %s"%proc.returncode)',

        '    try:',

        '        with socket.create_connection(("127.0.0.1",PORT),timeout=2): print("[F5] Gradio socket ready"); break',

        '    except OSError: time.sleep(3)',

        'else: raise TimeoutError("F5-TTS did not start within 15 minutes")',

        'print("[NGROK] Connecting static domain"); ngrok.set_auth_token(NGROK_AUTH_TOKEN)',

        'tunnel=ngrok.connect(addr=PORT,proto="http",domain=NGROK_DOMAIN); public_url=tunnel.public_url',

        'print("[NGROK] Public URL:",public_url)',

        'deadline=time.time()+180',

        'while time.time()<deadline:',

        '    try:',

        '        r=requests.get(public_url,timeout=10)',

        '        if r.status_code==200 and "gradio" in r.text.lower(): print("F5-TTS NODE ONLINE"); print("PUBLIC_URL:",public_url); break',

        '    except Exception as e: print("[HEALTH] Waiting:", e)',

        '    time.sleep(5)',

        'else: raise RuntimeError("ngrok domain failed Gradio health check")',

        # ----------------------------------------------------
        # IMPORTANT:
        # 20 MINUTES STARTS AFTER PUBLIC_URL IS READY.
        # ----------------------------------------------------

        'print("[SESSION] F5-TTS READY. 20-minute session timer started.")',

        'session_deadline=time.time()+1200',

        'while proc.poll() is None and time.time()<session_deadline: time.sleep(10)',

        'if proc.poll() is None:',

        '    print("[SESSION] 20-minute session limit reached. Stopping F5-TTS.")',

        '    try: proc.terminate()',

        '    except Exception: pass',

        '    try: proc.wait(timeout=15)',

        '    except Exception:',

        '        try: proc.kill()',

        '        except Exception: pass',

        'print("[F5] Process exited:",proc.returncode)',
    ]

    code = "\n".join(
        code_lines
    )

    code = code.replace(
        "__TOKEN__",
        repr(token),
    )

    code = code.replace(
        "__DOMAIN__",
        repr(domain),
    )

    code = code.replace(
        "__VOICES__",
        repr(bundle),
    )

    (
        folder / "main.py"
    ).write_text(
        code,
        encoding="utf-8",
    )

    metadata = {
        "id": "",
        "title": "F5-TTS Cloud Hub",
        "code_file": "main.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        "machine_shape": "NvidiaTeslaT4",
    }

    (
        folder
        / "kernel-metadata.json"
    ).write_text(
        json.dumps(
            metadata,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# DEPLOY
# ============================================================

def deploy(user):
    # Server-side protection.
    existing = get_deployment(
        user.id
    )

    if (
        existing
        and existing.status
        in ACTIVE_DEPLOYMENT_STATES
    ):
        st.info(
            "An F5-TTS session is already active."
        )
        return False

    c = get_credentials(
        user.id
    )

    if not c:
        st.error(
            "Save Settings first."
        )
        return False

    username = c[
        "kaggle_username"
    ]

    token = c[
        "kaggle_token"
    ]

    ngrok_token = c[
        "ngrok_token"
    ]

    domain = normalize_domain(
        c["ngrok_domain"]
    )

    if not all(
        (
            username,
            token,
            ngrok_token,
            domain,
        )
    ):
        st.error(
            "Complete all Settings fields."
        )
        return False

    slug = (
        "f5-tts-cloud-hub"
    )

    kernel_id = (
        f"{username}/{slug}"
    )

    with tempfile.TemporaryDirectory(
        prefix="f5tts-"
    ) as tmp:
        folder = Path(tmp)

        build_kernel(
            folder,
            ngrok_token,
            domain,
            list_voices(user.id),
        )

        meta = json.loads(
            (
                folder
                / "kernel-metadata.json"
            ).read_text()
        )

        meta["id"] = kernel_id

        (
            folder
            / "kernel-metadata.json"
        ).write_text(
            json.dumps(
                meta,
                indent=2,
            )
        )

        upsert_deployment(
            user.id,
            kernel_id=kernel_id,
            kernel_slug=slug,
            status="SUBMITTING",
            public_url=None,
            last_error=None,
            last_logs=(
                "Submitting to Kaggle..."
            ),
        )

        code, out = run_kaggle(
            [
                "kernels",
                "push",
                "-p",
                str(folder),
                "--accelerator",
                "NvidiaTeslaT4",
            ],
            username,
            token,
            180,
        )

    if code:
        upsert_deployment(
            user.id,
            kernel_id=kernel_id,
            kernel_slug=slug,
            status="ERROR",
            last_error=out[-12000:],
            last_logs=out[-30000:],
        )

        st.error(
            "Kaggle submission failed."
        )

        return False

    upsert_deployment(
        user.id,
        kernel_id=kernel_id,
        kernel_slug=slug,
        status="QUEUED",
        public_url=None,
        last_error=None,
        last_logs=out[-30000:],
    )

    return True


# ============================================================
# REFRESH DEPLOYMENT
# ============================================================

def refresh(user):
    dep = get_deployment(
        user.id
    )

    c = get_credentials(
        user.id
    )

    if not dep or not c:
        return dep

    # --------------------------------------------------------
    # READY SESSION:
    # Do not stream logs again.
    # Just maintain the persistent 20-minute session.
    # --------------------------------------------------------

    if (
        dep.status == "READY"
        and dep.public_url
    ):
        remaining = (
            deployment_seconds_left(
                dep
            )
        )

        # If marker exists and timer expired,
        # cancel the Kaggle session.
        if (
            remaining is not None
            and remaining <= 0
        ):
            username = c[
                "kaggle_username"
            ]

            token = c[
                "kaggle_token"
            ]

            try:
                stop_kaggle_session(
                    username,
                    token,
                    dep.kernel_slug,
                )
            except Exception:
                pass

            upsert_deployment(
                user.id,
                kernel_id=dep.kernel_id,
                kernel_slug=dep.kernel_slug,
                status="IDLE",
                public_url=None,
                last_error=None,
                last_logs=None,
            )

            return get_deployment(
                user.id
            )

        # READY remains stable.
        # No log fetch.
        return dep

    kaggle_user = c[
        "kaggle_username"
    ]

    kaggle_token = c[
        "kaggle_token"
    ]

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    try:
        s, stext = status(
            dep.kernel_id,
            kaggle_user,
            kaggle_token,
        )

    except Exception as exc:
        s = "UNKNOWN"
        stext = (
            f"[STATUS FETCH ERROR] "
            f"{exc}"
        )

    # --------------------------------------------------------
    # LOGS ARE INTERNAL ONLY.
    # Never rendered to UI.
    # --------------------------------------------------------

    try:
        lg = logs(
            dep.kernel_id,
            kaggle_user,
            kaggle_token,
        )

    except Exception as exc:
        lg = (
            f"[LOG FETCH ERROR] "
            f"{exc}"
        )

    combined = (
        lg + "\n" + stext
    ).strip()

    # --------------------------------------------------------
    # FIND PUBLIC URL
    # --------------------------------------------------------

    url = dep.public_url

    matches = re.findall(
        r"PUBLIC_URL\s*:\s*(https?://[^\s]+)",
        combined,
        flags=re.IGNORECASE,
    )

    if matches:
        url = matches[-1].rstrip(
            ").,;\"'"
        )

    # --------------------------------------------------------
    # FALLBACK NGROK URL
    # --------------------------------------------------------

    if not url:
        ngrok_matches = re.findall(
            r"https?://[A-Za-z0-9._-]+\.ngrok(?:-free)?\.app",
            combined,
            flags=re.IGNORECASE,
        )

        if ngrok_matches:
            url = ngrok_matches[-1].rstrip(
                ").,;\"'"
            )

    # --------------------------------------------------------
    # DETERMINE STATE
    # --------------------------------------------------------

    if url:
        final = "READY"

    elif s == "ERROR":
        final = "ERROR"

    elif s == "RUNNING":
        final = "RUNNING"

    elif s == "QUEUED":
        final = "QUEUED"

    elif s == "COMPLETE":
        final = "COMPLETE"

    else:
        final = s

    # --------------------------------------------------------
    # READY TIMESTAMP
    # Only create it when first becoming READY.
    # --------------------------------------------------------

    if (
        final == "READY"
        and url
    ):
        ready_at = (
            extract_ready_timestamp(
                dep
            )
        )

        if ready_at is None:
            ready_at = time.time()

        stored_logs = (
            f"{make_ready_marker(ready_at)}\n"
            f"{combined[-29900:]}"
        )

        upsert_deployment(
            user.id,
            kernel_id=dep.kernel_id,
            kernel_slug=dep.kernel_slug,
            status="READY",
            public_url=url,
            last_error=None,
            last_logs=stored_logs[-30000:],
        )

        return get_deployment(
            user.id
        )

    # --------------------------------------------------------
    # NON-READY STATE
    # --------------------------------------------------------

    upsert_deployment(
        user.id,
        kernel_id=dep.kernel_id,
        kernel_slug=dep.kernel_slug,
        status=final,
        public_url=url,
        last_error=(
            stext
            if final == "ERROR"
            else None
        ),
        last_logs=combined[-30000:],
    )

    return get_deployment(
        user.id
    )


# ============================================================
# ADMIN SETTINGS
# ============================================================

def _get_admin_secret(
    name,
    default="",
):
    try:
        value = st.secrets.get(
            name,
            "",
        )

        if value:
            return str(
                value
            ).strip()

    except Exception:
        pass

    return os.getenv(
        name,
        default,
    ).strip()


ADMIN_USERNAME = _get_admin_secret(
    "ADMIN_USERNAME"
)

ADMIN_PASSWORD = _get_admin_secret(
    "ADMIN_PASSWORD"
)

ADMIN_SESSION_SECRET = _get_admin_secret(
    "ADMIN_SESSION_SECRET"
)

WHATSAPP_NUMBER = (
    "923097647772"
)

WHATSAPP_MESSAGE = (
    "Assalam-o-Alaikum, I would like to get assistance regarding "
    "my ZAIKO AI STUDIO account. Please let me know how I can proceed. "
    "Thank you."
)


def whatsapp_url():
    from urllib.parse import quote

    return (
        f"https://wa.me/{WHATSAPP_NUMBER}"
        f"?text={quote(WHATSAPP_MESSAGE)}"
    )


def _admin_cookie_value():
    import hashlib
    import hmac

    if (
        not ADMIN_SESSION_SECRET
        or not ADMIN_USERNAME
    ):
        return ""

    return hmac.new(
        ADMIN_SESSION_SECRET.encode(),
        ADMIN_USERNAME.encode(),
        hashlib.sha256,
    ).hexdigest()


def is_admin():
    try:
        if st.session_state.get(
            "admin_authenticated",
            False,
        ):
            return True

        cookie_value = None

        try:
            cookie_value = cookies.get(
                "f5tts_admin"
            )
        except Exception:
            pass

        if not cookie_value:
            try:
                all_cookies = (
                    cookies.get_all()
                )

                if isinstance(
                    all_cookies,
                    dict,
                ):
                    cookie_value = (
                        all_cookies.get(
                            "f5tts_admin"
                        )
                    )

            except Exception:
                pass

        if (
            ADMIN_USERNAME
            and ADMIN_PASSWORD
            and ADMIN_SESSION_SECRET
            and cookie_value
            and cookie_value
            == _admin_cookie_value()
        ):
            st.session_state[
                "admin_authenticated"
            ] = True

            return True

    except Exception:
        pass

    return False


def admin_login():
    st.session_state[
        "admin_authenticated"
    ] = True

    try:
        cookie_value = (
            _admin_cookie_value()
        )

        if cookie_value:
            cookies.set(
                "f5tts_admin",
                cookie_value,
                expires_at=(
                    time.time()
                    + SESSION_DAYS
                    * 86400
                ),
            )

    except Exception:
        pass


def admin_logout():
    st.session_state[
        "admin_authenticated"
    ] = False

    try:
        cookies.delete(
            "f5tts_admin"
        )

        cookies.delete(
            "f5tts_admin_page"
        )

    except Exception:
        pass

    st.rerun()


# ============================================================
# BRANDING
# ============================================================

def branding_css():
    st.markdown(
        """
        <style>
        .stApp {
            background:#000000;
            color:#f4f7ff;
        }

        [data-testid="stSidebar"] {
            background:#050505;
            border-right:1px solid #111827;
        }

        .brand {
            font-size:38px;
            font-weight:900;
            letter-spacing:4px;
            color:#2196ff;
            text-align:center;
            margin:20px 0 4px;
        }

        .brand-sub {
            text-align:center;
            color:#8ea8c7;
            margin-bottom:28px;
        }

        .footer {
            text-align:center;
            margin-top:60px;
            padding:24px 0;
            border-top:1px solid #111827;
            color:#8090a5;
        }

        .footer a {
            display:inline-block;
            margin-top:12px;
            padding:10px 18px;
            border-radius:9px;
            background:#1683ff;
            color:white!important;
            text-decoration:none;
            font-weight:700;
        }

        .client-card {
            padding:18px;
            border:1px solid #182234;
            border-radius:14px;
            background:#070b12;
            margin-bottom:12px;
        }

        .countdown {
            font-size:27px;
            font-weight:800;
            color:#35a7ff;
        }

        .f5-ready {
            padding:14px 16px;
            border:1px solid #123c24;
            border-radius:12px;
            background:#06150c;
            margin-bottom:12px;
        }

        .f5-progress {
            padding:16px;
            border:1px solid #182234;
            border-radius:12px;
            background:#070b12;
            margin:12px 0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def footer():
    st.markdown(
        f'<div class="footer">'
        f'<div>Built by M Zakriya</div>'
        f'<a href="{whatsapp_url()}" target="_blank">'
        f"Contact on WhatsApp"
        f"</a>"
        f"</div>",
        unsafe_allow_html=True,
    )


def brand_header():
    st.markdown(
        '<div class="brand">'
        "ZAIKO AI STUDIO"
        "</div>",
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div class="brand-sub">'
        "AI Voice & F5-TTS Cloud Platform"
        "</div>",
        unsafe_allow_html=True,
    )


# ============================================================
# CLIENT ACCESS
# ============================================================

def client_access_seconds(user):
    from datetime import datetime, timezone

    if not user.access_expires_at:
        return 0

    return max(
        0,
        int(
            (
                user.access_expires_at
                - datetime.now(
                    timezone.utc
                )
            ).total_seconds()
        ),
    )


def format_countdown(seconds):
    days, rem = divmod(
        max(0, seconds),
        86400,
    )

    hours, rem = divmod(
        rem,
        3600,
    )

    minutes, secs = divmod(
        rem,
        60,
    )

    return (
        f"{days} days "
        f"{hours:02d} hours "
        f"{minutes:02d} minutes "
        f"{secs:02d} seconds"
    )


# ============================================================
# LOGIN
# ============================================================

def client_login_page():
    brand_header()

    st.markdown(
        "### Sign in to your account"
    )

    with st.form(
        "login",
        clear_on_submit=False,
    ):
        username = st.text_input(
            "Username",
            autocomplete="username",
        )

        password = st.text_input(
            "Password",
            type="password",
            autocomplete="current-password",
        )

        submitted = (
            st.form_submit_button(
                "Sign in",
                type="primary",
                use_container_width=True,
            )
        )

    if submitted:
        username_clean = (
            username.strip()
        )

        if (
            ADMIN_USERNAME
            and ADMIN_PASSWORD
            and username_clean
            == ADMIN_USERNAME
            and password
            == ADMIN_PASSWORD
        ):
            admin_login()
            st.rerun()
            return

        try:
            user = authenticate_user(
                username_clean,
                password,
            )

        except Exception as exc:
            st.error(
                "Unable to check your login."
            )

            st.exception(exc)

            footer()
            return

        if user is None:
            st.error(
                "Invalid username or password, "
                "or your client access is expired/revoked."
            )

            footer()
            return

        try:
            raw_session = (
                create_session(
                    user.id,
                    SESSION_DAYS,
                )
            )

            if not raw_session:
                st.error(
                    "Login succeeded, but a "
                    "client session could not be created."
                )

                footer()
                return

        except Exception as exc:
            st.error(
                "Login succeeded, but the "
                "client session could not be created."
            )

            st.exception(exc)

            footer()
            return

        st.session_state[
            "client_authenticated_user"
        ] = user

        st.session_state[
            "client_authenticated_user_id"
        ] = user.id

        st.session_state[
            "user_id"
        ] = user.id

        cookie_ok = cookie_set(
            raw_session
        )

        if not cookie_ok:
            st.warning(
                "Login successful. "
                "Your current session is active, but "
                "persistent browser login could not be saved."
            )

        st.success(
            "Login successful. "
            "Opening your dashboard..."
        )

        time.sleep(1)

        st.rerun()

    footer()


# ============================================================
# ADMIN DASHBOARD
# ============================================================

def admin_dashboard():
    stats = admin_stats()

    st.title(
        "Admin Dashboard"
    )

    st.caption(
        "ZAIKO AI STUDIO client management"
    )

    a, b, c, d = st.columns(4)

    a.metric(
        "Active Clients",
        stats["active"],
    )

    b.metric(
        "Revoked Clients",
        stats["revoked"],
    )

    c.metric(
        "Expired Access",
        stats["expired"],
    )

    d.metric(
        "Total Clients",
        stats["total"],
    )

    st.divider()

    st.subheader(
        "Client Overview"
    )

    users = admin_list_users(
        include_revoked=True
    )

    if not users:
        st.info(
            "No clients have been created yet."
        )

    for user in users[:12]:
        status_text = (
            "Active"
            if user.is_active
            else "Revoked"
        )

        expiry = (
            user.access_expires_at.strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            if user.access_expires_at
            else "No expiry"
        )

        st.markdown(
            f'<div class="client-card">'
            f"<b>{user.username}</b> — "
            f"{status_text}<br>"
            f"Access expiry: {expiry}"
            f"</div>",
            unsafe_allow_html=True,
        )


def active_clients():
    st.title(
        "Active Clients"
    )

    users = admin_list_users(
        include_revoked=False
    )

    if not users:
        st.info(
            "No active clients."
        )
        return

    for user in users:
        with st.container(
            border=True
        ):
            left, right = st.columns(
                [4, 1]
            )

            with left:
                st.markdown(
                    f"### {user.username}"
                )

                expiry = (
                    user.access_expires_at.strftime(
                        "%d %b %Y, %H:%M UTC"
                    )
                    if user.access_expires_at
                    else "No expiry"
                )

                st.caption(
                    f"Current expiry: {expiry}"
                )

            with right:
                if st.button(
                    "Revoke",
                    key=f"revoke-{user.id}",
                ):
                    admin_revoke_user(
                        user.id
                    )

                    st.success(
                        "Account revoked."
                    )

                    st.rerun()

            with st.expander(
                "Edit client"
            ):
                new_username = st.text_input(
                    "Username",
                    value=user.username,
                    key=f"un-{user.id}",
                )

                new_password = st.text_input(
                    "New password (leave blank to keep current)",
                    type="password",
                    key=f"pw-{user.id}",
                )

                current_days = max(
                    1,
                    client_access_seconds(
                        user
                    ) // 86400,
                )

                access_days = st.number_input(
                    "Access duration from now (days)",
                    min_value=1,
                    max_value=3650,
                    value=current_days,
                    key=f"days-{user.id}",
                )

                if st.button(
                    "Save Changes",
                    key=f"save-{user.id}",
                    type="primary",
                ):
                    ok, error = (
                        admin_update_user(
                            user.id,
                            username=new_username,
                            password=(
                                new_password
                                if new_password
                                else None
                            ),
                            access_days=int(
                                access_days
                            ),
                        )
                    )

                    if ok:
                        st.success(
                            "Changes saved and applied."
                        )

                        st.rerun()

                    else:
                        st.error(
                            error
                            or "Unable to save changes."
                        )


def revoked_clients():
    st.title(
        "Revoked Clients"
    )

    users = [
        u
        for u in admin_list_users(
            include_revoked=True
        )
        if not u.is_active
    ]

    if not users:
        st.info(
            "No revoked clients."
        )
        return

    for user in users:
        with st.container(
            border=True
        ):
            st.markdown(
                f"### {user.username}"
            )

            st.caption(
                "Access revoked. Client credentials, "
                "deployment and saved voices were cleared."
            )

            days = st.number_input(
                "Grant access (days)",
                min_value=1,
                max_value=3650,
                value=7,
                key=f"grant-days-{user.id}",
            )

            if st.button(
                "Grant Access",
                key=f"grant-{user.id}",
                type="primary",
            ):
                ok, error = (
                    admin_grant_access(
                        user.id,
                        int(days),
                    )
                )

                if ok:
                    st.success(
                        "Access granted. Client is active again."
                    )

                    st.rerun()

                else:
                    st.error(
                        error
                        or "Unable to grant access."
                    )


def create_new_client():
    st.title(
        "Create New Client"
    )

    with st.form(
        "new-client"
    ):
        username = st.text_input(
            "Client username"
        )

        password = st.text_input(
            "Client password",
            type="password",
        )

        access_days = st.number_input(
            "Access duration (days)",
            min_value=1,
            max_value=3650,
            value=7,
        )

        submitted = (
            st.form_submit_button(
                "Save Changes / Create Client",
                type="primary",
                use_container_width=True,
            )
        )

    if submitted:
        ok, error = (
            admin_create_client(
                username,
                password,
                int(access_days),
            )
        )

        if ok:
            st.success(
                f"Client '{username.strip()}' "
                "created and activated."
            )

        else:
            st.error(
                error
                or "Unable to create client."
            )


# ============================================================
# ADMIN PANEL
# ============================================================

def admin_panel():
    brand_header()

    admin_pages = [
        "Dashboard",
        "Active Clients",
        "Revoked Clients",
        "Create New Client",
    ]

    saved_admin_page = (
        page_cookie_get(
            "f5tts_admin_page",
            "Dashboard",
        )
    )

    if (
        saved_admin_page
        not in admin_pages
    ):
        saved_admin_page = (
            "Dashboard"
        )

    with st.sidebar:
        st.markdown(
            "### ZAIKO AI STUDIO"
        )

        page = st.radio(
            "Admin Menu",
            admin_pages,
            index=admin_pages.index(
                saved_admin_page
            ),
            key="admin_page",
        )

        page_cookie_set(
            "f5tts_admin_page",
            page,
        )

        st.divider()

        if st.button(
            "Admin Logout",
            use_container_width=True,
        ):
            admin_logout()

    if page == "Dashboard":
        admin_dashboard()

    elif page == "Active Clients":
        active_clients()

    elif page == "Revoked Clients":
        revoked_clients()

    else:
        create_new_client()

    footer()


# ============================================================
# CLIENT SETTINGS
# ============================================================

def client_settings(user):
    st.title(
        "Settings"
    )

    creds = (
        get_credentials(
            user.id
        )
        or {}
    )

    with st.form(
        "settings"
    ):
        ku = st.text_input(
            "Kaggle username",
            value=creds.get(
                "kaggle_username",
                "",
            ),
        )

        kt = st.text_input(
            "Kaggle API token",
            value=creds.get(
                "kaggle_token",
                "",
            ),
            type="password",
        )

        nt = st.text_input(
            "ngrok auth token",
            value=creds.get(
                "ngrok_token",
                "",
            ),
            type="password",
        )

        nd = st.text_input(
            "ngrok static domain",
            value=creds.get(
                "ngrok_domain",
                "",
            ),
        )

        ok = st.form_submit_button(
            "Save settings",
            type="primary",
            use_container_width=True,
        )

    if ok:
        nd = normalize_domain(
            nd
        )

        if not all(
            (
                ku,
                kt,
                nt,
                nd,
            )
        ):
            st.error(
                "All fields are required."
            )

        else:
            save_credentials(
                user.id,
                ku,
                kt,
                nt,
                nd,
            )

            st.success(
                "Settings saved securely."
            )

    st.divider()

    st.subheader(
        "Saved Voices"
    )

    upload = st.file_uploader(
        "Reference voice",
        type=[
            "wav",
            "mp3",
            "flac",
            "m4a",
            "ogg",
        ],
    )

    name = st.text_input(
        "Voice name"
    )

    if st.button(
        "Save Voice",
        use_container_width=True,
    ):
        if (
            not upload
            or not name.strip()
        ):
            st.error(
                "Choose an audio file and "
                "enter a voice name."
            )

        else:
            save_voice(
                user.id,
                name,
                upload.name,
                upload.type,
                upload.getvalue(),
            )

            st.success(
                "Voice saved."
            )

            st.rerun()

    for row in list_voices(
        user.id
    ):
        x, y = st.columns(
            [5, 1]
        )

        x.write(
            f"**{row.name}** — "
            f"`{row.filename}`"
        )

        if y.button(
            "Delete",
            key=f"voice-{row.id}",
        ):
            delete_voice(
                user.id,
                row.id,
            )

            st.rerun()


# ============================================================
# CLIENT DASHBOARD
# ============================================================

def client_dashboard(user):
    st.title(
        f"Welcome, {user.username} 👋"
    )

    st.caption(
        "Your ZAIKO AI STUDIO dashboard"
    )

    # ========================================================
    # PLAN COUNTDOWN
    # Browser-side only.
    # No Streamlit reruns every second.
    # ========================================================

    seconds = client_access_seconds(
        user
    )

    expiry_text = (
        user.access_expires_at.strftime(
            "%d %b %Y, %I:%M:%S %p UTC"
        )
        if user.access_expires_at
        else "No expiry"
    )

    st.subheader(
        "Your Plan"
    )

    if seconds <= 0:
        st.error(
            "Your access has expired. "
            "Please contact the administrator."
        )

    else:
        st.write(
            f"Access expires: **{expiry_text}**"
        )

        countdown_id = (
            "plan-countdown"
        )

        st.markdown(
            f"""
            <div
                id="{countdown_id}"
                class="countdown"
            >
                {format_countdown(seconds)}
            </div>

            <script>
            (function() {{
                let remaining = {seconds};

                function updatePlanCountdown() {{
                    const el =
                        document.getElementById(
                            "{countdown_id}"
                        );

                    if (!el) return;

                    if (remaining <= 0) {{
                        el.innerText =
                            "0 days 00 hours 00 minutes 00 seconds";
                        return;
                    }}

                    const days =
                        Math.floor(
                            remaining / 86400
                        );

                    let rem =
                        remaining % 86400;

                    const hours =
                        Math.floor(
                            rem / 3600
                        );

                    rem %= 3600;

                    const minutes =
                        Math.floor(
                            rem / 60
                        );

                    const seconds =
                        rem % 60;

                    el.innerText =
                        days + " days "
                        + String(hours).padStart(2, "0")
                        + " hours "
                        + String(minutes).padStart(2, "0")
                        + " minutes "
                        + String(seconds).padStart(2, "0")
                        + " seconds";

                    remaining--;
                }}

                updatePlanCountdown();

                setInterval(
                    updatePlanCountdown,
                    1000
                );
            }})();
            </script>
            """,
            unsafe_allow_html=True,
        )

    st.divider()

    # ========================================================
    # GET CURRENT DEPLOYMENT
    # ========================================================

    dep = get_deployment(
        user.id
    )

    # ========================================================
    # IMPORTANT:
    # Refresh only when we actually need deployment monitoring.
    # READY does NOT call refresh repeatedly.
    # ========================================================

    if (
        dep
        and dep.status
        in {
            "SUBMITTING",
            "QUEUED",
            "RUNNING",
        }
    ):
        active_session = True

    elif (
        dep
        and dep.status == "READY"
        and dep.public_url
    ):
        active_session = True

    else:
        active_session = False

    # ========================================================
    # DEPLOY BUTTON
    # ========================================================

    deploy_clicked = st.button(
        "🚀 Deploy / Restart F5-TTS",
        type="primary",
        use_container_width=True,
        disabled=active_session,
    )

    if deploy_clicked:
        with st.spinner(
            "Starting F5-TTS on Kaggle..."
        ):
            ok = deploy(user)

        if ok:
            st.rerun()

    # ========================================================
    # READY STATE
    # ========================================================

    if (
        dep
        and dep.status == "READY"
        and dep.public_url
    ):
        remaining = (
            deployment_seconds_left(
                dep
            )
        )

        if (
            remaining is not None
            and remaining <= 0
        ):
            dep = refresh(user)
            remaining = (
                deployment_seconds_left(
                    dep
                )
            )

        if (
            dep
            and dep.status == "READY"
            and dep.public_url
        ):
            st.markdown(
                '<div class="f5-ready">'
                '<b>F5-TTS is ready.</b><br>'
                "Your voice generation system is live."
                "</div>",
                unsafe_allow_html=True,
            )

            # ------------------------------------------------
            # 20-MINUTE SESSION COUNTDOWN
            # Browser-side only.
            # ------------------------------------------------

            timer_seconds = (
                remaining
                if remaining is not None
                else F5_SESSION_SECONDS
            )

            st.markdown(
                f"""
                <div style="
                    padding:10px 0;
                    color:#8ea8c7;
                ">
                    Session time remaining:
                    <span
                        id="f5-session-countdown"
                        style="
                            color:#35a7ff;
                            font-weight:800;
                        "
                    >
                        {format_countdown(timer_seconds)}
                    </span>
                </div>

                <script>
                (function() {{
                    let remaining =
                        {timer_seconds};

                    function updateF5Timer() {{
                        const el =
                            document.getElementById(
                                "f5-session-countdown"
                            );

                        if (!el) return;

                        if (remaining <= 0) {{
                            el.innerText =
                                "Session ending...";
                            return;
                        }}

                        const days =
                            Math.floor(
                                remaining / 86400
                            );

                        let rem =
                            remaining % 86400;

                        const hours =
                            Math.floor(
                                rem / 3600
                            );

                        rem %= 3600;

                        const minutes =
                            Math.floor(
                                rem / 60
                            );

                        const seconds =
                            rem % 60;

                        el.innerText =
                            days + " days "
                            + String(hours).padStart(2, "0")
                            + " hours "
                            + String(minutes).padStart(2, "0")
                            + " minutes "
                            + String(seconds).padStart(2, "0")
                            + " seconds";

                        remaining--;
                    }}

                    updateF5Timer();

                    setInterval(
                        updateF5Timer,
                        1000
                    );
                }})();
                </script>
                """,
                unsafe_allow_html=True,
            )

            # ------------------------------------------------
            # START VOICE GENERATION
            # EXACT LABEL PRESERVED
            # ------------------------------------------------

            st.link_button(
                "🎙️ Start voice generation",
                dep.public_url,
                use_container_width=True,
            )

            st.divider()

            # ------------------------------------------------
            # STOP BUTTON
            # ------------------------------------------------

            if st.button(
                "⏹ Stop F5-TTS session",
                use_container_width=True,
            ):
                creds = get_credentials(
                    user.id
                )

                if not creds:
                    st.error(
                        "Kaggle settings are missing."
                    )

                else:
                    with st.spinner(
                        "Stopping F5-TTS GPU session..."
                    ):
                        ok, message = (
                            stop_kaggle_session(
                                creds[
                                    "kaggle_username"
                                ],
                                creds[
                                    "kaggle_token"
                                ],
                                dep.kernel_slug,
                            )
                        )

                    if ok:
                        upsert_deployment(
                            user.id,
                            kernel_id=dep.kernel_id,
                            kernel_slug=dep.kernel_slug,
                            status="IDLE",
                            public_url=None,
                            last_error=None,
                            last_logs=None,
                        )

                        st.success(
                            "F5-TTS session stopped. "
                            "GPU session has been released."
                        )

                        time.sleep(0.5)

                        st.rerun()

                    else:
                        st.error(
                            "Unable to stop the Kaggle session."
                        )

                        # Do NOT show Kaggle logs.
                        st.caption(
                            "Kaggle did not accept the session-stop request."
                        )

            # ------------------------------------------------
            # CRITICAL:
            # NO deployment_monitor here.
            # READY PAGE IS STABLE.
            # ------------------------------------------------

            return

    # ========================================================
    # NO ACTIVE DEPLOYMENT
    # ========================================================

    if not dep:
        st.info(
            "No F5-TTS deployment yet."
        )
        return

    # ========================================================
    # ERROR
    # ========================================================

    if dep.status == "ERROR":
        st.error(
            "Kaggle kernel reported an error. "
            "You can deploy again."
        )
        return

    # ========================================================
    # COMPLETE
    # ========================================================

    if dep.status == "COMPLETE":
        st.info(
            "The previous F5-TTS session has ended. "
            "Deploy again to start a new session."
        )
        return

    # ========================================================
    # ACTIVE MONITOR
    #
    # Logs are NOT rendered.
    # Only progress/status is shown.
    # When PUBLIC_URL appears, full app reruns once.
    # Then this fragment disappears because state is READY.
    # ========================================================

    if (
        dep.status
        in {
            "SUBMITTING",
            "QUEUED",
            "RUNNING",
        }
    ):

        if hasattr(
            st,
            "fragment",
        ):

            @st.fragment(
                run_every=float(
                    POLL_SECONDS
                )
            )
            def deployment_monitor():
                with st.spinner(
                    "Checking F5-TTS deployment..."
                ):
                    current = refresh(
                        user
                    )

                if not current:
                    st.info(
                        "Waiting for F5-TTS deployment..."
                    )
                    return

                if (
                    current.status
                    == "READY"
                    and current.public_url
                ):
                    # PUBLIC_URL detected.
                    # Stop this monitoring fragment
                    # and perform one clean full rerun.
                    st.rerun()

                elif current.status == "ERROR":
                    st.error(
                        "Kaggle kernel reported an error."
                    )

                elif current.status == "RUNNING":
                    st.info(
                        "Kaggle GPU is running. "
                        "F5-TTS is starting..."
                    )

                elif current.status == "QUEUED":
                    st.info(
                        "Kaggle GPU is queued. "
                        "Waiting for F5-TTS..."
                    )

                elif current.status == "SUBMITTING":
                    st.info(
                        "Submitting F5-TTS to Kaggle..."
                    )

                else:
                    st.info(
                        "Starting F5-TTS..."
                    )

            deployment_monitor()

        else:
            with st.spinner(
                "Checking F5-TTS deployment..."
            ):
                dep = refresh(
                    user
                )

            if (
                dep
                and dep.status == "READY"
                and dep.public_url
            ):
                st.rerun()

            elif dep and dep.status == "ERROR":
                st.error(
                    "Kaggle kernel reported an error."
                )

            elif dep and dep.status == "RUNNING":
                st.info(
                    "Kaggle GPU is running. "
                    "F5-TTS is starting..."
                )

            elif dep and dep.status == "QUEUED":
                st.info(
                    "Kaggle GPU is queued. "
                    "Waiting for F5-TTS..."
                )

            else:
                st.info(
                    "Starting F5-TTS..."
                )

        return

    # ========================================================
    # IDLE / UNKNOWN
    # ========================================================

    st.info(
        "No active F5-TTS session."
    )


# ============================================================
# CLIENT PANEL
# ============================================================

def client_panel(user):
    st.session_state[
        "user_id"
    ] = user.id

    client_pages = [
        "Dashboard",
        "Settings",
    ]

    saved_client_page = (
        page_cookie_get(
            "f5tts_client_page",
            "Dashboard",
        )
    )

    if (
        saved_client_page
        not in client_pages
    ):
        saved_client_page = (
            "Dashboard"
        )

    with st.sidebar:
        st.markdown(
            "### ZAIKO AI STUDIO"
        )

        page = st.radio(
            "Menu",
            client_pages,
            index=client_pages.index(
                saved_client_page
            ),
            key="client_page",
        )

        page_cookie_set(
            "f5tts_client_page",
            page,
        )

        st.divider()

        if st.button(
            "Log out",
            use_container_width=True,
        ):
            logout()

    if page == "Dashboard":
        client_dashboard(
            user
        )

    else:
        client_settings(
            user
        )

    footer()


# ============================================================
# MAIN
# ============================================================

def main():
    branding_css()

    # ========================================================
    # AUTH COOKIE BOOTSTRAP
    # ========================================================

    if not st.session_state.get(
        "_auth_cookie_bootstrap_done",
        False,
    ):
        try:
            admin_cookie = None
            client_cookie = None

            try:
                admin_cookie = cookies.get(
                    "f5tts_admin"
                )
            except Exception:
                pass

            try:
                client_cookie = cookies.get(
                    "f5tts_session"
                )
            except Exception:
                pass

            if (
                not admin_cookie
                and not client_cookie
            ):
                try:
                    all_cookies = (
                        cookies.get_all()
                    )

                    if isinstance(
                        all_cookies,
                        dict,
                    ):
                        admin_cookie = (
                            all_cookies.get(
                                "f5tts_admin"
                            )
                        )

                        client_cookie = (
                            all_cookies.get(
                                "f5tts_session"
                            )
                        )

                except Exception:
                    pass

            st.session_state[
                "_auth_cookie_bootstrap_done"
            ] = True

            if (
                not admin_cookie
                and not client_cookie
            ):
                st.rerun()

        except Exception:
            st.session_state[
                "_auth_cookie_bootstrap_done"
            ] = True

    # ========================================================
    # ADMIN AUTHENTICATION
    # ========================================================

    if is_admin():
        admin_panel()
        return

    # ========================================================
    # CLIENT AUTHENTICATION
    # ========================================================

    user = (
        st.session_state.get(
            "client_authenticated_user"
        )
    )

    # ========================================================
    # RESTORE CLIENT FROM COOKIE
    # ========================================================

    if user is None:
        raw_cookie = cookie_get()

        if raw_cookie:
            user = (
                get_user_by_session(
                    raw_cookie
                )
            )

            if user:
                st.session_state[
                    "client_authenticated_user"
                ] = user

                st.session_state[
                    "client_authenticated_user_id"
                ] = user.id

                st.session_state[
                    "user_id"
                ] = user.id

    # ========================================================
    # NO CLIENT SESSION
    # ========================================================

    if not user:
        client_login_page()
        return

    # ========================================================
    # CLIENT ACCESS VALIDATION
    # ========================================================

    from datetime import datetime, timezone

    if (
        not user.is_active
        or (
            user.access_expires_at
            and user.access_expires_at
            <= datetime.now(
                timezone.utc
            )
        )
    ):
        st.session_state.pop(
            "client_authenticated_user",
            None,
        )

        st.session_state.pop(
            "client_authenticated_user_id",
            None,
        )

        raw_cookie = cookie_get()

        if raw_cookie:
            delete_session(
                raw_cookie
            )

        try:
            cookies.delete(
                "f5tts_session"
            )
        except Exception:
            pass

        st.error(
            "Your access has expired or has been revoked. "
            "Please contact the administrator."
        )

        footer()
        return

    st.session_state[
        "user_id"
    ] = user.id

    client_panel(
        user
    )


# ============================================================
# APP START
# ============================================================

try:
    main()

except Exception as exc:
    st.error(
        "Application error"
    )

    st.exception(exc)
