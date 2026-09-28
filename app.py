import base64, json, os, re, subprocess, tempfile, time
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

cookies = stx.CookieManager(key="f5tts-cookie")


def normalize_domain(v):
    v = v.strip()

    if not v:
        return ""

    if not v.startswith(("http://", "https://")):
        v = "https://" + v

    p = urlparse(v)

    return (
        p.netloc or p.path.split("/")[0]
    ).lower().rstrip("/")


def cookie_get():
    try:
        return cookies.get("f5tts_session")
    except Exception:
        return None


def cookie_set(v):
    try:
        cookies.set(
            "f5tts_session",
            v,
            expires_at=time.time() + SESSION_DAYS * 86400,
        )
    except Exception:
        pass


def logout():
    delete_session(cookie_get())

    try:
        cookies.delete("f5tts_session")
    except Exception:
        pass

    st.rerun()


def run_kaggle(args, user, token, timeout=90):
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

    return p.returncode, (
        p.stdout or ""
    ) + (
        p.stderr or ""
    )


def status(kernel, user, token):
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
            else "ERROR"
        ), out

    if "running" in low:
        return "RUNNING", out

    if "queued" in low or "pending" in low:
        return "QUEUED", out

    if "complete" in low or "success" in low:
        return "COMPLETE", out

    if "error" in low or "failed" in low:
        return "ERROR", out

    return "UNKNOWN", out


def logs(kernel, user, token):
    _, out = run_kaggle(
        ["kernels", "logs", kernel],
        user,
        token,
        60,
    )

    return out[-30000:]


def find_kernel(user, token, slug):
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
                return f"{user}/{m.group(1)}"

    return None


def build_kernel(folder, token, domain, voices):
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
        'print("[BOOT] Installing F5-TTS and ngrok"); subprocess.run(["pip","install","-q","f5-tts","pyngrok"],check=True)',
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
        '    except Exception as e: print("[HEALTH] Waiting:",e)',
        '    time.sleep(5)',
        'else: raise RuntimeError("ngrok domain failed Gradio health check")',
        'while proc.poll() is None: time.sleep(10)',
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

    (folder / "main.py").write_text(
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

    (folder / "kernel-metadata.json").write_text(
        json.dumps(
            metadata,
            indent=2,
        ),
        encoding="utf-8",
    )


def deploy(user):
    c = get_credentials(user.id)

    if not c:
        st.error("Save Settings first.")
        return

    username = c["kaggle_username"]
    token = c["kaggle_token"]
    ngrok_token = c["ngrok_token"]
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
        return

    dep = get_deployment(user.id)

    slug = (
        f"f5-tts-"
        f"{re.sub(r'[^a-z0-9-]+', '-', user.username.lower()).strip('-')}"
        f"-{user.id}"
    )[:90]

    if dep and dep.kernel_id.startswith(
        username + "/"
    ):
        kernel_id = dep.kernel_id
        slug = dep.kernel_slug

    else:
        kernel_id = f"{username}/{slug}"

        found = find_kernel(
            username,
            token,
            slug,
        )

        if found:
            kernel_id, slug = (
                found,
                found.split("/", 1)[1],
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

        st.code(
            out[-12000:]
        )

        return

    upsert_deployment(
        user.id,
        kernel_id=kernel_id,
        kernel_slug=slug,
        status="QUEUED",
        public_url=None,
        last_error=None,
        last_logs=out[-30000:],
    )

    st.rerun()


def refresh(user):
    dep = get_deployment(user.id)
    c = get_credentials(user.id)

    if not dep or not c:
        return dep

    s, stext = status(
        dep.kernel_id,
        c["kaggle_username"],
        c["kaggle_token"],
    )

    lg = logs(
        dep.kernel_id,
        c["kaggle_username"],
        c["kaggle_token"],
    )

    combined = (
        lg + "\n" + stext
    ).strip()

    url = dep.public_url

    m = re.search(
        r"PUBLIC_URL:\s*(https?://\S+)",
        combined,
    )

    if m:
        url = m.group(1).rstrip(
            ")., "
        )

    final = s

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
                final = "READY"
            else:
                final = "RUNNING"

        except Exception:
            final = "RUNNING"

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

    return get_deployment(user.id)


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
        # First check the current Streamlit session.
        if st.session_state.get(
            "admin_authenticated",
            False,
        ):
            return True

        # Then check the persistent browser cookie.
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
    # Mark admin as authenticated immediately.
    st.session_state[
        "admin_authenticated"
    ] = True

    # Also save persistent cookie.
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
    # Clear current Streamlit session.
    st.session_state[
        "admin_authenticated"
    ] = False

    # Clear persistent admin cookie.
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


def client_login_page():
    brand_header()

    st.markdown(
        "### Sign in to your account"
    )

    with st.form("login"):
        username = st.text_input(
            "Username",
            autocomplete="username",
        )

        password = st.text_input(
            "Password",
            type="password",
            autocomplete="current-password",
        )

        submitted = st.form_submit_button(
            "Sign in",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        if (
            ADMIN_USERNAME
            and ADMIN_PASSWORD
            and username.strip()
            == ADMIN_USERNAME
            and password
            == ADMIN_PASSWORD
        ):
            admin_login()
            st.rerun()

        user = authenticate_user(
            username,
            password,
        )

        if not user:
            st.error(
                "Login not authorized. "
                "Please contact the administrator."
            )

        else:
            cookie_set(
                create_session(
                    user.id,
                    SESSION_DAYS,
                )
            )

            st.rerun()

    footer()


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

        submitted = st.form_submit_button(
            "Save Changes / Create Client",
            type="primary",
            use_container_width=True,
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


def admin_panel():
    brand_header()

    with st.sidebar:
        st.markdown(
            "### ZAIKO AI STUDIO"
        )

        page = st.radio(
            "Admin Menu",
            [
                "Dashboard",
                "Active Clients",
                "Revoked Clients",
                "Create New Client",
            ],
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


def client_settings(user):
    st.title(
        "Settings"
    )

    creds = (
        get_credentials(user.id)
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
        nd = normalize_domain(nd)

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


def client_dashboard(user):
    st.title(
        f"Welcome, {user.username} 👋"
    )

    st.caption(
        "Your ZAIKO AI STUDIO dashboard"
    )

    if hasattr(
        st,
        "fragment",
    ):

        @st.fragment(
            run_every="1s"
        )
        def countdown_fragment():
            st.subheader(
                "Your Plan"
            )

            seconds = (
                client_access_seconds(
                    user
                )
            )

            if seconds <= 0:
                st.error(
                    "Your access has expired. "
                    "Please contact the administrator."
                )

            else:
                expiry = (
                    user.access_expires_at.strftime(
                        "%d %b %Y, %I:%M:%S %p UTC"
                    )
                )

                st.write(
                    f"Access expires: **{expiry}**"
                )

                st.markdown(
                    f'<div class="countdown">'
                    f"{format_countdown(seconds)}"
                    f"</div>",
                    unsafe_allow_html=True,
                )

        countdown_fragment()

    else:
        seconds = (
            client_access_seconds(
                user
            )
        )

        st.subheader(
            "Your Plan"
        )

        st.write(
            "Access expires: **"
            f"{user.access_expires_at.strftime('%d %b %Y, %I:%M:%S %p UTC')}"
            "**"
        )

        st.markdown(
            f'<div class="countdown">'
            f"{format_countdown(seconds)}"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.divider()

    if st.button(
        "🚀 Deploy / Restart F5-TTS",
        type="primary",
        use_container_width=True,
    ):
        deploy(user)

    dep = refresh(user)

    if not dep:
        st.info(
            "No F5-TTS deployment yet."
        )
        return

    st.metric(
        "Kaggle Session",
        dep.status or "IDLE",
    )

    if (
        dep.status == "READY"
        and dep.public_url
    ):
        st.success(
            "F5-TTS, Gradio and ngrok are online."
        )

        st.link_button(
            "🎙️ Open F5-TTS",
            dep.public_url,
            use_container_width=True,
        )

    elif dep.status == "ERROR":
        st.error(
            "Kaggle kernel reported an error."
        )

        st.code(
            (dep.last_error or "")[
                -12000:
            ]
        )

    else:
        st.info(
            "Waiting for GPU → F5-TTS → "
            "Gradio → ngrok health checks."
        )

    st.subheader(
        "Kaggle Logs"
    )

    st.code(
        (
            dep.last_logs
            or "Waiting for logs..."
        )[-30000:],
        language="text",
    )


def client_panel(user):
    with st.sidebar:
        st.markdown(
            "### ZAIKO AI STUDIO"
        )

        page = st.radio(
            "Menu",
            [
                "Dashboard",
                "Settings",
            ],
        )

        st.divider()

        if st.button(
            "Log out",
            use_container_width=True,
        ):
            logout()

    if page == "Dashboard":
        client_dashboard(user)

    else:
        client_settings(user)

    footer()


def main():
    branding_css()

    if is_admin():
        admin_panel()
        return

    user = get_user_by_session(
        cookie_get()
    )

    if not user:
        client_login_page()
        return

    from datetime import datetime, timezone

    if (
        not user.is_active
        or (
            user.access_expires_at
            and user.access_expires_at
            <= datetime.now(timezone.utc)
        )
    ):
        delete_session(
            cookie_get()
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

    client_panel(user)


try:
    main()

except Exception as exc:
    st.error(
        "Application error"
    )

    st.exception(exc)
