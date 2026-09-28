import os
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    access_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )

    credentials = relationship(
        "Credential",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    deployments = relationship(
        "Deployment",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    sessions = relationship(
        "LoginSession",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    voices = relationship(
        "SavedVoice",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        unique=True,
        index=True,
    )
    kaggle_username_enc: Mapped[str] = mapped_column(Text)
    kaggle_token_enc: Mapped[str] = mapped_column(Text)
    ngrok_token_enc: Mapped[str] = mapped_column(Text)
    ngrok_domain_enc: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    user = relationship("User", back_populates="credentials")


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        unique=True,
        index=True,
    )
    kernel_id: Mapped[str] = mapped_column(String(200), unique=True)
    kernel_slug: Mapped[str] = mapped_column(String(120))
    public_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(40),
        default="IDLE",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    last_logs: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    user = relationship("User", back_populates="deployments")


class LoginSession(Base):
    __tablename__ = "login_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )

    user = relationship("User", back_populates="sessions")


class SavedVoice(Base):
    __tablename__ = "saved_voices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
    )
    encrypted_audio: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )

    user = relationship("User", back_populates="voices")


def _get_secret(name):
    """
    Read a value from Streamlit Secrets when running on Streamlit Cloud.
    Fall back to environment variables for local development.
    """
    try:
        import streamlit as st

        value = st.secrets.get(name, "")
        if value:
            return str(value).strip()
    except Exception:
        pass

    return os.getenv(name, "").strip()


def _db_url():
    url = _get_secret("DATABASE_URL")

    if not url:
        raise RuntimeError("DATABASE_URL is missing.")

    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[11:]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[13:]

    return url


def _fernet():
    key = _get_secret("APP_ENCRYPTION_KEY")

    if not key:
        raise RuntimeError("APP_ENCRYPTION_KEY is missing.")

    try:
        return Fernet(key.encode())
    except Exception as e:
        raise RuntimeError("Invalid APP_ENCRYPTION_KEY.") from e


ENGINE = create_engine(
    _db_url(),
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=ENGINE,
    expire_on_commit=False,
)


def init_db():
    Base.metadata.create_all(ENGINE)

    # Lightweight migration for databases created by the previous version.
    inspector = inspect(ENGINE)
    columns = {
        c["name"]
        for c in inspector.get_columns("users")
    }

    with ENGINE.begin() as conn:
        if "access_expires_at" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE users "
                    "ADD COLUMN access_expires_at "
                    "TIMESTAMP WITH TIME ZONE"
                )
            )

        if "revoked_at" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE users "
                    "ADD COLUMN revoked_at "
                    "TIMESTAMP WITH TIME ZONE"
                )
            )


def _hash_password(password):
    salt = secrets.token_bytes(16)
    rounds = 390000

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt,
        rounds,
    )

    return (
        f"pbkdf2_sha256${rounds}$"
        f"{base64.urlsafe_b64encode(salt).decode()}$"
        f"{base64.urlsafe_b64encode(digest).decode()}"
    )


def _verify_password(password, stored):
    try:
        scheme, rounds, salt, digest = stored.split("$", 3)

        if scheme != "pbkdf2_sha256":
            return False

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            base64.urlsafe_b64decode(salt),
            int(rounds),
        )

        return hmac.compare_digest(
            actual,
            base64.urlsafe_b64decode(digest),
        )

    except Exception:
        return False


def create_user(username, password):
    username = username.strip().lower()

    if len(username) < 3 or len(username) > 80:
        return None, "Username must be 3-80 characters."

    if len(password) < 8:
        return None, "Password must be at least 8 characters."

    with SessionLocal() as db:
        if db.query(User).filter_by(username=username).first():
            return None, "Username already exists."

        user = User(
            username=username,
            password_hash=_hash_password(password),
            access_expires_at=utcnow() + timedelta(days=30),
        )

        db.add(user)
        db.commit()
        db.refresh(user)

        return user, None


def authenticate_user(username, password):
    with SessionLocal() as db:
        user = (
            db.query(User)
            .filter_by(
                username=username.strip().lower(),
                is_active=True,
            )
            .first()
        )

        if not user:
            return None

        if not _verify_password(
            password,
            user.password_hash,
        ):
            return None

        if not user.is_active:
            return None

        if (
            user.access_expires_at
            and user.access_expires_at <= utcnow()
        ):
            return None

        return user


# ============================================================
# ADDED ONLY FOR CLIENT LOGIN SESSION FIX
# ============================================================
def get_user_by_id(user_id):
    try:
        with SessionLocal() as db:
            return (
                db.query(User)
                .filter(
                    User.id == user_id,
                    User.is_active == True,
                )
                .first()
            )
    except Exception:
        return None


def create_session(user_id, days=30):
    raw = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(
        raw.encode()
    ).hexdigest()

    with SessionLocal() as db:
        db.add(
            LoginSession(
                user_id=user_id,
                token_hash=token_hash,
                expires_at=utcnow() + timedelta(days=days),
            )
        )

        db.commit()

    return raw


def get_user_by_session(raw):
    if not raw:
        return None

    with SessionLocal() as db:
        row = (
            db.query(LoginSession)
            .filter_by(
                token_hash=hashlib.sha256(
                    raw.encode()
                ).hexdigest()
            )
            .first()
        )

        if not row or row.expires_at <= utcnow():
            return None

        return (
            db.query(User)
            .filter_by(
                id=row.user_id,
                is_active=True,
            )
            .first()
        )


def delete_session(raw):
    if not raw:
        return

    with SessionLocal() as db:
        (
            db.query(LoginSession)
            .filter_by(
                token_hash=hashlib.sha256(
                    raw.encode()
                ).hexdigest()
            )
            .delete()
        )

        db.commit()


def save_credentials(
    user_id,
    kaggle_username,
    kaggle_token,
    ngrok_token,
    ngrok_domain,
):
    f = _fernet()

    values = {
        "kaggle_username_enc": f.encrypt(
            kaggle_username.strip().encode()
        ).decode(),

        "kaggle_token_enc": f.encrypt(
            kaggle_token.strip().encode()
        ).decode(),

        "ngrok_token_enc": f.encrypt(
            ngrok_token.strip().encode()
        ).decode(),

        "ngrok_domain_enc": f.encrypt(
            ngrok_domain.strip().encode()
        ).decode(),
    }

    with SessionLocal() as db:
        row = (
            db.query(Credential)
            .filter_by(user_id=user_id)
            .first()
        )

        if not row:
            db.add(
                Credential(
                    user_id=user_id,
                    **values,
                )
            )
        else:
            for k, v in values.items():
                setattr(row, k, v)

        db.commit()


def get_credentials(user_id):
    with SessionLocal() as db:
        row = (
            db.query(Credential)
            .filter_by(user_id=user_id)
            .first()
        )

        if not row:
            return None

        f = _fernet()

        try:
            return {
                k: f.decrypt(
                    getattr(row, k + "_enc").encode()
                ).decode()
                for k in [
                    "kaggle_username",
                    "kaggle_token",
                    "ngrok_token",
                    "ngrok_domain",
                ]
            }

        except InvalidToken as e:
            raise RuntimeError(
                "Cannot decrypt stored credentials. "
                "Check APP_ENCRYPTION_KEY."
            ) from e


def get_deployment(user_id):
    with SessionLocal() as db:
        return (
            db.query(Deployment)
            .filter_by(user_id=user_id)
            .first()
        )


def upsert_deployment(user_id, **values):
    with SessionLocal() as db:
        row = (
            db.query(Deployment)
            .filter_by(user_id=user_id)
            .first()
        )

        if not row:
            row = Deployment(
                user_id=user_id,
                **values,
            )
            db.add(row)
        else:
            for k, v in values.items():
                setattr(row, k, v)

        db.commit()
        db.refresh(row)

        return row


def save_voice(
    user_id,
    name,
    filename,
    mime_type,
    audio_bytes,
):
    with SessionLocal() as db:
        row = SavedVoice(
            user_id=user_id,
            name=name.strip()[:120],
            filename=filename[:255],
            mime_type=mime_type,
            encrypted_audio=_fernet().encrypt(
                audio_bytes
            ),
        )

        db.add(row)
        db.commit()
        db.refresh(row)

        return row


def list_voices(user_id):
    with SessionLocal() as db:
        return (
            db.query(SavedVoice)
            .filter_by(user_id=user_id)
            .order_by(SavedVoice.created_at.desc())
            .all()
        )


def get_voice(user_id, voice_id):
    with SessionLocal() as db:
        row = (
            db.query(SavedVoice)
            .filter_by(
                id=voice_id,
                user_id=user_id,
            )
            .first()
        )

        if not row:
            return None

        return {
            "id": row.id,
            "name": row.name,
            "filename": row.filename,
            "mime_type": row.mime_type,
            "audio_bytes": _fernet().decrypt(
                row.encrypted_audio
            ),
        }


def delete_voice(user_id, voice_id):
    with SessionLocal() as db:
        row = (
            db.query(SavedVoice)
            .filter_by(
                id=voice_id,
                user_id=user_id,
            )
            .first()
        )

        if row:
            db.delete(row)
            db.commit()
            return True

        return False


def admin_list_users(include_revoked=False):
    with SessionLocal() as db:
        q = db.query(User).order_by(
            User.created_at.desc()
        )

        if not include_revoked:
            now = utcnow()

            q = q.filter(
                User.is_active == True
            ).filter(
                (User.access_expires_at.is_(None))
                | (User.access_expires_at > now)
            )

        return q.all()


def admin_get_user(user_id):
    with SessionLocal() as db:
        return (
            db.query(User)
            .filter_by(id=user_id)
            .first()
        )


def admin_update_user(
    user_id,
    username=None,
    password=None,
    access_days=None,
):
    with SessionLocal() as db:
        user = (
            db.query(User)
            .filter_by(id=user_id)
            .first()
        )

        if not user:
            return False, "Client not found."

        if username:
            username = username.strip().lower()

            duplicate = (
                db.query(User)
                .filter(
                    User.username == username,
                    User.id != user_id,
                )
                .first()
            )

            if duplicate:
                return False, "Username already exists."

            user.username = username

        if password:
            if len(password) < 8:
                return False, (
                    "Password must be at least 8 characters."
                )

            user.password_hash = _hash_password(
                password
            )

        if access_days is not None:
            user.access_expires_at = (
                utcnow()
                + timedelta(
                    days=max(0, int(access_days))
                )
            )
            user.is_active = True
            user.revoked_at = None

        db.commit()

        return True, None


def admin_create_client(
    username,
    password,
    access_days,
):
    username = username.strip().lower()

    if len(username) < 3 or len(username) > 80:
        return False, (
            "Username must be 3-80 characters."
        )

    if len(password) < 8:
        return False, (
            "Password must be at least 8 characters."
        )

    if int(access_days) < 1:
        return False, (
            "Access must be at least 1 day."
        )

    with SessionLocal() as db:
        if (
            db.query(User)
            .filter_by(username=username)
            .first()
        ):
            return False, "Username already exists."

        user = User(
            username=username,
            password_hash=_hash_password(password),
            is_active=True,
            access_expires_at=(
                utcnow()
                + timedelta(days=int(access_days))
            ),
        )

        db.add(user)
        db.commit()

        return True, None


def admin_revoke_user(user_id):
    with SessionLocal() as db:
        user = (
            db.query(User)
            .filter_by(id=user_id)
            .first()
        )

        if not user:
            return False

        user.is_active = False
        user.revoked_at = utcnow()

        (
            db.query(LoginSession)
            .filter_by(user_id=user_id)
            .delete()
        )

        (
            db.query(Credential)
            .filter_by(user_id=user_id)
            .delete()
        )

        (
            db.query(Deployment)
            .filter_by(user_id=user_id)
            .delete()
        )

        (
            db.query(SavedVoice)
            .filter_by(user_id=user_id)
            .delete()
        )

        db.commit()

        return True


def admin_grant_access(
    user_id,
    access_days,
    password=None,
    username=None,
):
    with SessionLocal() as db:
        user = (
            db.query(User)
            .filter_by(id=user_id)
            .first()
        )

        if not user:
            return False, "Client not found."

        if username:
            username = username.strip().lower()

            duplicate = (
                db.query(User)
                .filter(
                    User.username == username,
                    User.id != user_id,
                )
                .first()
            )

            if duplicate:
                return False, "Username already exists."

            user.username = username

        if password:
            if len(password) < 8:
                return False, (
                    "Password must be at least 8 characters."
                )

            user.password_hash = _hash_password(
                password
            )

        user.is_active = True
        user.revoked_at = None
        user.access_expires_at = (
            utcnow()
            + timedelta(
                days=max(1, int(access_days))
            )
        )

        db.commit()

        return True, None


def admin_stats():
    now = utcnow()

    with SessionLocal() as db:
        users = db.query(User).all()

        active = [
            u
            for u in users
            if u.is_active
            and (
                u.access_expires_at is None
                or u.access_expires_at > now
            )
        ]

        revoked = [
            u
            for u in users
            if not u.is_active
        ]

        expired = [
            u
            for u in users
            if u.is_active
            and u.access_expires_at
            and u.access_expires_at <= now
        ]

        return {
            "total": len(users),
            "active": len(active),
            "revoked": len(revoked),
            "expired": len(expired),
        }
