import base64, json, os, re, subprocess, tempfile, time
from pathlib import Path
from urllib.parse import urlparse
import requests
import streamlit as st
import extra_streamlit_components as stx
from database import authenticate_user, create_session, create_user, delete_session, delete_voice, get_credentials, get_deployment, get_user_by_session, get_voice, init_db, list_voices, save_credentials, save_voice, upsert_deployment

APP_NAME="F5-TTS Cloud Hub"; SESSION_DAYS=int(os.getenv("SESSION_DAYS","30")); POLL_SECONDS=int(os.getenv("POLL_SECONDS","5")); SIGNUP_CODE=os.getenv("SIGNUP_CODE","").strip()
st.set_page_config(page_title=APP_NAME,page_icon="🎙️",layout="wide")

@st.cache_resource
def boot(): init_db(); return True
boot()
cookies=stx.CookieManager(key="f5tts-cookie")

def normalize_domain(v):
    v=v.strip()
    if not v:return ""
    if not v.startswith(("http://","https://")):v="https://"+v
    p=urlparse(v); return (p.netloc or p.path.split("/")[0]).lower().rstrip("/")

def cookie_get():
    try:return cookies.get("f5tts_session")
    except:return None

def cookie_set(v):
    try:cookies.set("f5tts_session",v,expires_at=time.time()+SESSION_DAYS*86400)
    except:pass

def logout():
    delete_session(cookie_get())
    try:cookies.delete("f5tts_session")
    except:pass
    st.rerun()

def run_kaggle(args,user,token,timeout=90):
    env=os.environ.copy(); env["KAGGLE_USERNAME"]=user; env["KAGGLE_API_TOKEN"]=token
    p=subprocess.run(["kaggle",*args],capture_output=True,text=True,timeout=timeout,env=env)
    return p.returncode,(p.stdout or "")+(p.stderr or "")

def status(kernel,user,token):
    code,out=run_kaggle(["kernels","status",kernel],user,token,45); low=out.lower()
    if code:
        return ("NOT_FOUND" if any(x in low for x in ("not found","404","does not exist","could not find")) else "ERROR"),out
    if "running" in low:return "RUNNING",out
    if "queued" in low or "pending" in low:return "QUEUED",out
    if "complete" in low or "success" in low:return "COMPLETE",out
    if "error" in low or "failed" in low:return "ERROR",out
    return "UNKNOWN",out

def logs(kernel,user,token):
    _,out=run_kaggle(["kernels","logs",kernel],user,token,60); return out[-30000:]

def find_kernel(user,token,slug):
    code,out=run_kaggle(["kernels","list","--mine","--page-size","100"],user,token,60)
    if code:return None
    for line in out.splitlines():
        if slug.lower() in line.lower():
            m=re.search(rf"{re.escape(user)}/([A-Za-z0-9_-]+)",line)
            if m:return f"{user}/{m.group(1)}"
    return None

def build_kernel(folder,token,domain,voices):
    bundle=[]
    for row in voices:
        v=get_voice(st.session_state.user_id,row.id)
        if v:bundle.append({"name":v["name"],"filename":v["filename"],"data":base64.b64encode(v["audio_bytes"]).decode()})
    code_lines=['import base64, socket, subprocess, threading, time', 'from pathlib import Path', 'import requests', 'from pyngrok import ngrok', 'PORT=7860', 'NGROK_AUTH_TOKEN=__TOKEN__', 'NGROK_DOMAIN=__DOMAIN__', 'VOICE_BUNDLE=__VOICES__', 'voice_dir=Path("saved_voices"); voice_dir.mkdir(exist_ok=True)', 'for item in VOICE_BUNDLE:', '    try: (voice_dir/item["filename"]).write_bytes(base64.b64decode(item["data"])); print("[VOICE] Restored:",item["name"])', '    except Exception as e: print("[VOICE] Restore failed:",e)', 'print("[BOOT] Kaggle kernel started"); subprocess.run(["nvidia-smi"],check=False)', 'print("[BOOT] Installing F5-TTS and ngrok"); subprocess.run(["pip","install","-q","f5-tts","pyngrok"],check=True)', 'log=Path("f5tts.log"); handle=open(log,"a",buffering=1)', 'proc=subprocess.Popen(["f5-tts_infer-gradio","--host","0.0.0.0","--port",str(PORT)],stdout=handle,stderr=subprocess.STDOUT,text=True)', 'def relay():', '    pos=0', '    while proc.poll() is None:', '        try:', '            if log.exists():', '                with open(log,"r",encoding="utf-8",errors="replace") as f: f.seek(pos); chunk=f.read(); pos=f.tell()', '                for line in chunk.splitlines(): print("[F5]",line)', '        except Exception: pass', '        time.sleep(2)', 'threading.Thread(target=relay,daemon=True).start()', 'deadline=time.time()+900', 'while time.time()<deadline:', '    if proc.poll() is not None: raise RuntimeError("F5-TTS exited with code %s"%proc.returncode)', '    try:', '        with socket.create_connection(("127.0.0.1",PORT),timeout=2): print("[F5] Gradio socket ready"); break', '    except OSError: time.sleep(3)', 'else: raise TimeoutError("F5-TTS did not start within 15 minutes")', 'print("[NGROK] Connecting static domain"); ngrok.set_auth_token(NGROK_AUTH_TOKEN)', 'tunnel=ngrok.connect(addr=PORT,proto="http",domain=NGROK_DOMAIN); public_url=tunnel.public_url', 'print("[NGROK] Public URL:",public_url)', 'deadline=time.time()+180', 'while time.time()<deadline:', '    try:', '        r=requests.get(public_url,timeout=10)', '        if r.status_code==200 and "gradio" in r.text.lower(): print("F5-TTS NODE ONLINE"); print("PUBLIC_URL:",public_url); break', '    except Exception as e: print("[HEALTH] Waiting:",e)', '    time.sleep(5)', 'else: raise RuntimeError("ngrok domain failed Gradio health check")', 'while proc.poll() is None: time.sleep(10)', 'print("[F5] Process exited:",proc.returncode)']
    code="\n".join(code_lines)
    code=code.replace("__TOKEN__",repr(token)).replace("__DOMAIN__",repr(domain)).replace("__VOICES__",repr(bundle))
    (folder/"main.py").write_text(code,encoding="utf-8")
    metadata={"id":"","title":"F5-TTS Cloud Hub","code_file":"main.py","language":"python","kernel_type":"script","is_private":True,"enable_gpu":True,"enable_internet":True,"machine_shape":"NvidiaTeslaT4"}
    (folder/"kernel-metadata.json").write_text(json.dumps(metadata,indent=2),encoding="utf-8")

def deploy(user):
    c=get_credentials(user.id)
    if not c:st.error("Save Settings first.");return
    username=c["kaggle_username"]; token=c["kaggle_token"]; ngrok_token=c["ngrok_token"]; domain=normalize_domain(c["ngrok_domain"])
    if not all((username,token,ngrok_token,domain)):st.error("Complete all Settings fields.");return
    dep=get_deployment(user.id); slug=f"f5-tts-{re.sub(r'[^a-z0-9-]+','-',user.username.lower()).strip('-')}-{user.id}"[:90]
    if dep and dep.kernel_id.startswith(username+"/"):kernel_id,slug=dep.kernel_id,dep.kernel_slug
    else:
        kernel_id=f"{username}/{slug}"; found=find_kernel(username,token,slug)
        if found:kernel_id,slug=found,found.split("/",1)[1]
    with tempfile.TemporaryDirectory(prefix="f5tts-") as tmp:
        folder=Path(tmp); build_kernel(folder,ngrok_token,domain,list_voices(user.id))
        meta=json.loads((folder/"kernel-metadata.json").read_text());meta["id"]=kernel_id;(folder/"kernel-metadata.json").write_text(json.dumps(meta,indent=2))
        upsert_deployment(user.id,kernel_id=kernel_id,kernel_slug=slug,status="SUBMITTING",public_url=None,last_error=None,last_logs="Submitting to Kaggle...")
        code,out=run_kaggle(["kernels","push","-p",str(folder),"--accelerator","NvidiaTeslaT4"],username,token,180)
    if code:
        upsert_deployment(user.id,kernel_id=kernel_id,kernel_slug=slug,status="ERROR",last_error=out[-12000:],last_logs=out[-30000:])
        st.error("Kaggle submission failed.");st.code(out[-12000:]);return
    upsert_deployment(user.id,kernel_id=kernel_id,kernel_slug=slug,status="QUEUED",public_url=None,last_error=None,last_logs=out[-30000:]);st.rerun()

def refresh(user):
    dep=get_deployment(user.id);c=get_credentials(user.id)
    if not dep or not c:return dep
    s,stext=status(dep.kernel_id,c["kaggle_username"],c["kaggle_token"]); lg=logs(dep.kernel_id,c["kaggle_username"],c["kaggle_token"]); combined=(lg+"\n"+stext).strip()
    url=dep.public_url;m=re.search(r"PUBLIC_URL:\s*(https?://\S+)",combined)
    if m:url=m.group(1).rstrip(")., ")
    final=s
    if "F5-TTS NODE ONLINE" in combined and url:
        try: final="READY" if requests.get(url,timeout=12).status_code==200 and "gradio" in requests.get(url,timeout=12).text.lower() else "RUNNING"
        except:final="RUNNING"
    upsert_deployment(user.id,kernel_id=dep.kernel_id,kernel_slug=dep.kernel_slug,status=final,public_url=url,last_error=stext if final=="ERROR" else None,last_logs=combined[-30000:])
    return get_deployment(user.id)

def login():
    st.title("🎙️ F5-TTS Cloud Hub");st.caption("Kaggle T4 + F5-TTS + ngrok")
    a,b=st.tabs(["Sign in","Create account"])
    with a:
        with st.form("login"):
            u=st.text_input("Username");p=st.text_input("Password",type="password");ok=st.form_submit_button("Sign in",use_container_width=True)
        if ok:
            user=authenticate_user(u,p)
            if not user:st.error("Invalid username or password.")
            else:cookie_set(create_session(user.id,SESSION_DAYS));st.rerun()
    with b:
        if not SIGNUP_CODE:st.info("Signup is disabled.")
        else:
            with st.form("signup"):
                u=st.text_input("New username");p=st.text_input("Password",type="password");p2=st.text_input("Confirm password",type="password");code=st.text_input("Signup code",type="password");ok=st.form_submit_button("Create account",use_container_width=True)
            if ok:
                if code!=SIGNUP_CODE:st.error("Invalid signup code.")
                elif p!=p2:st.error("Passwords do not match.")
                else:
                    _,err=create_user(u,p)
                    if err:st.error(err)
                    else:st.success("Account created. Sign in now.")

def settings(user):
    st.title("⚙️ Settings");c=get_credentials(user.id) or {}
    with st.form("settings"):
        ku=st.text_input("Kaggle username",value=c.get("kaggle_username",""));kt=st.text_input("Kaggle API token",value=c.get("kaggle_token",""),type="password");nt=st.text_input("ngrok auth token",value=c.get("ngrok_token",""),type="password");nd=st.text_input("ngrok static domain",value=c.get("ngrok_domain",""));ok=st.form_submit_button("Save settings",type="primary",use_container_width=True)
    if ok:
        nd=normalize_domain(nd)
        if not all((ku,kt,nt,nd)):st.error("All fields are required.")
        else:save_credentials(user.id,ku,kt,nt,nd);st.success("Settings saved.")
    st.divider();st.subheader("Saved voices");up=st.file_uploader("Reference voice",type=["wav","mp3","flac","m4a","ogg"]);name=st.text_input("Voice name")
    if st.button("Save voice",use_container_width=True):
        if not up or not name.strip():st.error("Choose audio and enter a name.")
        else:save_voice(user.id,name,up.name,up.type,up.getvalue());st.success("Voice saved.");st.rerun()
    for row in list_voices(user.id):
        x,y=st.columns([5,1]);x.write(f"**{row.name}** — `{row.filename}`")
        if y.button("Delete",key=f"v{row.id}"):delete_voice(user.id,row.id);st.rerun()

def dashboard(user):
    st.title("🎙️ Dashboard");a,b=st.columns([5,1]);a.caption(f"Logged in as **{user.username}**")
    if b.button("Log out"):logout()
    if st.button("🚀 Deploy / Restart F5-TTS",type="primary",use_container_width=True):deploy(user)
    dep=refresh(user);st.divider()
    if not dep:st.info("No deployment yet.");return
    st.metric("Kaggle session",dep.status or "IDLE")
    if dep.status=="READY" and dep.public_url:
        st.success("F5-TTS, Gradio and ngrok are online.");st.link_button("🎙️ Open F5-TTS",dep.public_url,use_container_width=True)
    elif dep.status=="ERROR":
        st.error("Kaggle kernel reported an error.");st.code((dep.last_error or "")[-12000:])
    else:st.info("Waiting for GPU → F5-TTS → Gradio → ngrok health checks.")
    st.subheader("Kaggle logs");st.code((dep.last_logs or "Waiting for logs...")[-30000:],language="text");st.caption(f"Target refresh interval: {POLL_SECONDS}s.")

def main():
    user=get_user_by_session(cookie_get())
    if not user:login();return
    st.session_state.user_id=user.id
    with st.sidebar:
        st.markdown("### F5-TTS Cloud Hub");page=st.radio("Navigation",["Dashboard","Settings"]);st.divider();st.caption("Credentials are encrypted in the database.")
    dashboard(user) if page=="Dashboard" else settings(user)

try:main()
except Exception as e:st.error("Application error");st.exception(e)
