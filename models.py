"""User model used by Flask-Login."""

from __future__ import annotations

import bcrypt
from flask_login import UserMixin

from core.database import (
    clear_password_reset_token,
    count_users,
    create_user,
    delete_user,
    get_user_by_email,
    get_user_by_google_id,
    get_user_by_id,
    get_user_by_identifier,
    get_user_by_reset_token_hash,
    list_users,
    set_password_reset_token,
    set_user_google_id,
    touch_user_login,
    update_user_access,
    update_user_role,
    update_user_verified,
    update_user_password,
)


class User(UserMixin):
    def __init__(self, **kwargs):
        self.id = kwargs.get("id")
        self.username = kwargs.get("username", "")
        self.email = kwargs.get("email", "")
        self.password_hash = kwargs.get("password_hash")
        self.google_id = kwargs.get("google_id")
        self.reset_token_hash = kwargs.get("reset_token_hash")
        self.reset_token_expires = kwargs.get("reset_token_expires")
        self.role = kwargs.get("role", "viewer")
        self.is_verified_flag = bool(kwargs.get("is_verified", kwargs.get("email_verified", False)))
        self.is_active_flag = bool(kwargs.get("is_active", True))
        self.created_at = kwargs.get("created_at")
        self.updated_at = kwargs.get("updated_at")
        self.last_login_at = kwargs.get("last_login_at")
        self.allowed_offices = kwargs.get("allowed_offices", []) or []
        self.allowed_modules = kwargs.get("allowed_modules", []) or []

    @property
    def is_active(self):
        return self.is_active_flag

    @property
    def is_verified(self):
        return self.is_verified_flag

    @property
    def email_verified(self):
        return self.is_verified_flag

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_manager(self):
        return self.role == "manager"

    @property
    def is_viewer(self):
        return self.role == "viewer"

    @classmethod
    def from_dict(cls, data):
        return cls(**data) if data else None

    @classmethod
    def get(cls, user_id):
        # هشدار: این متد روی «کلاس» صدا زده می‌شود (User.get(id) ← Flask-Login).
        # روی نمونه‌ها صدای user.get("role") — الگوی دیکشنری — به اشتباه به
        # همین classmethod می‌رسد؛ با پیام واضح جلوی تله‌ی زمان‌بر می‌شویم.
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            raise TypeError(
                f"User.get() سازنده‌ای کلاس‌متدی است و شناسه‌ی عددی کاربر می‌خواهد، "
                f"نه {user_id!r}. برای خواندن مقدار از نمونه‌ی User از صفت استفاده کنید "
                f"(مثلاً user.role به‌جای user.get('role'))."
            ) from None
        return cls.from_dict(get_user_by_id(uid))

    @classmethod
    def find_by_identifier(cls, identifier):
        return cls.from_dict(get_user_by_identifier(identifier))

    @classmethod
    def find_by_email(cls, email):
        return cls.from_dict(get_user_by_email(email))

    @classmethod
    def find_by_google_id(cls, google_id):
        return cls.from_dict(get_user_by_google_id(google_id))

    @classmethod
    def find_by_reset_token_hash(cls, token_hash):
        return cls.from_dict(get_user_by_reset_token_hash(token_hash))

    @classmethod
    def create(cls, username, email, password=None, google_id=None, role="viewer", is_verified=False, email_verified=None,
               allowed_offices=None, allowed_modules=None):
        password_hash = None
        if password:
            password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        if email_verified is not None:
            is_verified = bool(email_verified)
        return cls.from_dict(
            create_user(
                username=username,
                email=email,
                password_hash=password_hash,
                google_id=google_id,
                role=role,
                is_verified=is_verified,
                allowed_offices=allowed_offices,
                allowed_modules=allowed_modules,
            )
        )

    @classmethod
    def all(cls):
        return [cls.from_dict(row) for row in list_users()]

    @classmethod
    def total_count(cls):
        return count_users()

    def verify_password(self, password: str) -> bool:
        if not self.password_hash or not password:
            return False
        try:
            return bcrypt.checkpw(password.encode("utf-8"), self.password_hash.encode("utf-8"))
        except Exception:
            return False

    def set_password(self, password: str) -> bool:
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        ok = update_user_password(int(self.id), password_hash)
        if ok:
            self.password_hash = password_hash
        return ok

    def set_google_id(self, google_id: str) -> bool:
        ok = set_user_google_id(int(self.id), google_id)
        if ok:
            self.google_id = google_id
        return ok

    def set_role(self, role: str) -> bool:
        ok = update_user_role(int(self.id), role)
        if ok:
            self.role = role
        return ok

    def set_access(self, allowed_offices=None, allowed_modules=None) -> bool:
        ok = update_user_access(int(self.id), allowed_offices=allowed_offices, allowed_modules=allowed_modules)
        if ok:
            self.allowed_offices = list(allowed_offices or [])
            self.allowed_modules = list(allowed_modules or [])
        return ok

    def can_access_module(self, module_name: str) -> bool:
        if self.role == "admin":
            return True
        module_name = str(module_name or "").strip().lower()
        allowed = [str(item).strip().lower() for item in (self.allowed_modules or []) if str(item).strip()]
        return not allowed or module_name in allowed

    def can_access_office(self, office_id: str) -> bool:
        if self.role == "admin":
            return True
        office_id = str(office_id or "").strip().lower()
        allowed = [str(item).strip().lower() for item in (self.allowed_offices or []) if str(item).strip()]
        return not allowed or office_id in allowed

    def set_verified(self, is_verified: bool) -> bool:
        ok = update_user_verified(int(self.id), is_verified)
        if ok:
            self.is_verified_flag = bool(is_verified)
        return ok

    def touch_login(self):
        touch_user_login(int(self.id))

    def set_reset_token(self, token_hash: str, expires_at: str) -> bool:
        return set_password_reset_token(int(self.id), token_hash, expires_at)

    def clear_reset_token(self) -> bool:
        ok = clear_password_reset_token(int(self.id))
        if ok:
            self.reset_token_hash = None
            self.reset_token_expires = None
        return ok

    def delete(self) -> bool:
        return delete_user(int(self.id))
