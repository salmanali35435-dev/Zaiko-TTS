import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

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
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))
SIGNUP_CODE = os.getenv("SIGNUP_CODE", "").strip()


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
        p.netloc
        or p.path.split("/")[0]
    ).lower().rstrip("/")


def cookie_get():
    try:
        return cookies.get(
            "f5tts_session"
        )
    except Exception:
        return None


def cookie_set(v):
    try:
        cookies.set(
            "f5tts_session",
            v,
            expires_at=(
                time.time()
                + SESSION_DAYS * 86400
            ),
        )
    except Exception:
        pass


def logout():
    delete_session(
        cookie_get()
    )

    try:
        cookies.delete(
            "f5tts_session"
        )
    except Exception:
        pass

    st.rerun()


def run_kaggle(
    args,
    user,
    token,
    timeout=90,
):
    env = os.environ.copy()

    env["KAGGLE_USERNAME"] = user
    env["KAGGLE_API_TOKEN"] = token

    p = subprocess.run(
        ["kaggle", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )

    return (
        p.returncode,
        (p.stdout or "")
        + (p.stderr or ""),
    )


def status(
    kernel,
    user,
    token,
):
    code, out = run_kaggle(
        ["kernels", "status", kernel],
        user,
        token,
        45,
    )

    low = out.lower()

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
            else "ERROR",
            out,
        )

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


def logs(
    kernel,
    user,
    token,
):
    _, out = run_kaggle(
        ["kernels", "logs", kernel],
        user,
        token,
        60,
    )

    return out[-30000:]


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
            match = re.search(
                rf"{re.escape(user)}/([A-Za-z0-9_-]+)",
                line,
            )

            if match:
                return (
                    f"{user}/{match.group(1)}"
                )

    return None


def build_kernel(
    folder,
    token,
    domain,
    voices,
):
    bundle = []

    for row in voices:
        voice = get_voice(
            st.session_state.user_id,
            row.id,
        )

        if voice:
            bundle.append(
                {
                    "name": voice["name"],
                    "filename": voice["filename"],
                    "data": base64.b64encode(
                        voice["audio_bytes"]
                    ).decode(),
                }
            )

    code_lines = [
        "import base64, socket, subprocess, threading, time",
        "from pathlib import Path",
        "import requests",
        "from pyngrok import ngrok",
        "PORT=7860",
        "NGROK_AUTH_TOKEN=__TOKEN__",
        "NGROK_DOMAIN=__DOMAIN__",
        "VOICE_BUNDLE=__VOICES__",
        'voice_dir=Path("saved_voices"); voice_dir.mkdir(exist_ok=True)',
        "for item in VOICE_BUNDLE:",
        '    try: (voice_dir/item["filename"]).write_bytes(base64.b64decode(item["data"])); print("[VOICE] Restored:",item["name"])',
        '    except Exception as e: print("[VOICE] Restore failed:",e)',
        'print("[BOOT] Kaggle kernel started"); subprocess.run(["nvidia-smi"],check=False)',
        'print("[BOOT] Installing F5-TTS and ngrok"); subprocess.run(["pip","install","-q","f5-tts","pyngrok"],check=True)',
        'log=Path("f5tts.log"); handle=open(log,"a",buffering=1)',
        'proc=subprocess.Popen(["f5-tts_infer-gradio","--host","0.0.0.0","--port",str(PORT)],stdout=handle,stderr=subprocess.STDOUT,text=True)',
        "def relay():",
        "    pos=0",
        "    while proc.poll() is None:",
        "        try:",
        '            if log.exists():',
        '                with open(log,"r",encoding="utf-8",errors="replace") as f: f.seek(pos); chunk=f.read(); pos=f.tell()',
        '                for line in chunk.splitlines(): print("[F5]",line)',
        "        except Exception: pass",
        "        time.sleep(2)",
        "threading.Thread(target=relay,daemon=True).start()",
        "deadline=time.time()+900",
        'while time.time()<deadline:',
        '    if proc.poll() is not None: raise RuntimeError("F5-TTS exited with code %s"%proc.returncode)',
        '    try:',
        '        with socket.create_connection(("127.0.0.1",PORT),timeout=2): print("[F5] Gradio socket ready"); break',
        "    except OSError: time.sleep(3)",
        'else: raise TimeoutError("F5-TTS did not start within 15 minutes")',
        'print("[NGROK] Connecting static domain"); ngrok.set_auth_token(NGROK_AUTH_TOKEN)',
        'tunnel=ngrok.connect(addr=PORT,proto="http",domain=NGROK_DOMAIN); public_url=tunnel.public_url',
        'print("[NGROK] Public URL:",public_url)',
        "deadline=time.time()+180",
        "while time.time()<deadline:",
        "    try:",
        '        r=requests.get(public_url,timeout=10)',
        '        if r.status_code==200 and "gradio" in r.text.lower(): print("F5-TTS NODE ONLINE"); print("PUBLIC_URL:",public_url); break',
        "    except Exception as e: print("[HEALTH] Waiting:",e)",
        "    time.sleep(5)",
        'else: raise RuntimeError("ngrok domain failed Gradio health check")',
        "while proc.poll() is None: time.sleep(10)",
        'print("[F5] Process exited:",proc.returncode)',
    ]

    code = "\n".join(code_lines)

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
        folder / "kernel-metadata.json"
    ).write_text(
        json.dumps(
            metadata,
            indent=2,
        ),
        encoding="utf-8",
    )


def deploy(user):
    credentials = get_credentials(
        user.id
    )

    if not credentials:
        st.error(
            "Save Settings first."
        )
        return

    username = credentials[
        "kaggle_username"
    ]

    token = credentials[
        "kaggle_token"
    ]

    ngrok_token = credentials[
        "ngrok_token"
    ]

    domain = normalize_domain(
        credentials["ngrok_domain"]
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
        return

    deployment = get_deployment(
        user.id
    )

    slug = (
        f"f5-tts-"
        f"{re.sub(r'[^a-z0-9-]+', '-', user.username.lower()).strip('-')}"
        f"-{user.id}"
    )[:90]

    if (
        deployment
        and deployment.kernel_id.startswith(
            username + "/"
        )
    ):
        kernel_id = deployment.kernel_id
        slug = deployment.kernel_slug
    else:
        kernel_id = (
            f"{username}/{slug}"
        )

        found = find_kernel(
            username,
            token,
            slug,
        )

        if found:
            kernel_id = found
            slug = found.split(
                "/",
                1,
            )[1]

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

        metadata_path = (
            folder
            / "kernel-metadata.json"
        )

        metadata = json.loads(
            metadata_path.read_text()
        )

        metadata["id"] = kernel_id

        metadata_path.write_text(
            json.dumps(
                metadata,
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

        code, output = run_kaggle(
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
            last_error=output[-12000:],
            last_logs=output[-30000:],
        )

        st.error(
            "Kaggle submission failed."
        )

        st.code(
            output[-12000:]
        )

        return

    upsert_deployment(
        user.id,
        kernel_id=kernel_id,
        kernel_slug=slug,
        status="QUEUED",
        public_url=None,
        last_error=None,
        last_logs=output[-30000:],
    )

    st.rerun()


def refresh(user):
    deployment = get_deployment(
        user.id
    )

    credentials = get_credentials(
        user.id
    )

    if (
        not deployment
        or not credentials
    ):
        return deployment

    current_status, status_text = status(
        deployment.kernel_id,
        credentials["kaggle_username"],
        credentials["kaggle_token"],
    )

    log_text = logs(
        deployment.kernel_id,
        credentials["kaggle_username"],
        credentials["kaggle_token"],
    )

    combined = (
        log_text
        + "\n"
        + status_text
    ).strip()

    url = deployment.public_url

    match = re.search(
        r"PUBLIC_URL:\s*(https?://\S+)",
        combined,
    )

    if match:
        url = match.group(1).rstrip(
            ")., "
        )

    final_status = current_status

    if (
        "F5-TTS NODE ONLINE"
        in combined
        and url
    ):
        try:
            response = requests.get(
                url,
                timeout=12,
            )

            if (
                response.status_code == 200
                and "gradio"
                in response.text.lower()
            ):
                final_status = "READY"
            else:
                final_status = "RUNNING"

        except Exception:
            final_status = "RUNNING"

    upsert_deployment(
        user.id,
        kernel_id=deployment.kernel_id,
        kernel_slug=deployment.kernel_slug,
        status=final_status,
        public_url=url,
        last_error=(
            status_text
            if final_status == "ERROR"
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
            return str(value).strip()

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

WHATSAPP_NUMBER = "923097647772"

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

        cookie_value = cookies.get(
            "f5tts_admin"
        )

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
                    + SESSION_DAYS * 86400
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
    except Exception:
        pass

    st.rerun()


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


def client_access_seconds(user):
    from datetime import datetime, timezone

    if not user.access_expires_at:
        return 0

    return max(
        0,
        int(
            (
                user.access_expires_at
                - datetime.now(timezone.utc)
            ).total_seconds()
        ),
    )


def format_countdown(seconds):
    days, remainder = divmod(
        max(0, seconds),
        86400,
    )

    hours, remainder = divmod(
        remainder,
        3600,
    )

    minutes, secs = divmod(
        remainder,
        60,
    )

    return (
        f"{days} days "
        f"{hours:02d} hours "
        f"{minutes:02d} minutes "
        f"{secs:02d} seconds"
    )


# ============================================================
# CLIENT LOGIN
# ============================================================

def client_login_page():
    brand_header()

    st.markdown(
        "### Sign in to your account"
    )

    with st.form(
        "client_login_form",
        clear_on_submit=False,
    ):
        username = st.text_input(
            "Username",
            key="client_login_username",
            autocomplete="username",
        )

        password = st.text_input(
            "Password",
            type="password",
            key="client_login_password",
            autocomplete="current-password",
        )

        submitted = st.form_submit_button(
            "Sign in",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        username_clean = username.strip()

        # ----------------------------------------------------
        # ADMIN LOGIN
        # ----------------------------------------------------
        if (
            ADMIN_USERNAME
            and ADMIN_PASSWORD
            and username_clean
            == ADMIN_USERNAME
            and password
            == ADMIN_PASSWORD
        ):
            admin_login()
            st.r
