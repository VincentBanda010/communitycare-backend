"""Native phone forms backed by the owned identity service. No local password database."""
import hashlib
import hmac
import os
import threading
import time
from collections import OrderedDict
from enum import Enum
import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from .config import UID, https_url


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=256)


class Refresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refreshToken: SecretStr = Field(min_length=10, max_length=65536)


class Role(str, Enum):
    hsa = "HSA"
    nurse = "Nurse"
    clinical_officer = "Clinical Officer"
    supervisor = "Supervisor"


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fullName: str = Field(min_length=3, max_length=120)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str = Field(min_length=7, max_length=32, pattern=r"^[+0-9 ()-]+$")
    role: Role
    password: SecretStr = Field(min_length=8, max_length=256)

    @field_validator("fullName")
    @classmethod
    def complete_name(cls, value):
        value = " ".join(value.split())
        if len(value.split()) < 2:
            raise ValueError("Enter first and last name")
        return value


class Reset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=254)


class NativeAccounts:
    def __init__(self, issuer, client_id, secret, *, pilot_org=None, smtp=False, key=b"disabled", client=None, clock=time.monotonic):
        self.issuer = https_url(issuer)
        base, realm = self.issuer.rsplit("/realms/", 1)
        self.admin = base + "/admin/realms/" + realm
        self.client_id, self.secret = client_id, secret
        self.enabled = bool(client_id and secret)
        if pilot_org and not UID.fullmatch(pilot_org):
            raise ValueError("Invalid engineering pilot organisation")
        self.pilot_org, self.smtp, self.key = pilot_org, smtp, key
        self.client = client or httpx.Client(timeout=httpx.Timeout(20, connect=5), follow_redirects=False)
        self.clock = clock
        self.lock = threading.Lock()
        self.buckets = OrderedDict()

    @classmethod
    def from_env(cls):
        return cls(os.getenv("OIDC_ISSUER", "https://disabled.invalid/realms/communitycare"),
                   os.getenv("NATIVE_AUTH_CLIENT_ID", ""), os.getenv("NATIVE_AUTH_CLIENT_SECRET", ""),
                   pilot_org=os.getenv("NATIVE_REGISTRATION_PILOT_ORG") or None,
                   smtp=os.getenv("NATIVE_AUTH_SMTP_ENABLED") == "true",
                   key=os.getenv("SYNC_ENCRYPTION_KEY", "disabled").encode())

    def close(self):
        self.client.close()

    def limit(self, operation, peer, identity=""):
        if not self.enabled:
            raise HTTPException(503, "Account forms are not configured")
        checks = [(operation + ":peer:" + peer, 90, 60), (operation + ":identity:" + identity.lower(), 10, 60)]
        if operation == "register":
            checks.append(("register:total", 30, 3600))
        now = self.clock()
        with self.lock:
            for raw, maximum, seconds in checks:
                key = hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()
                start, count = self.buckets.get(key, (now, 0))
                if now - start >= seconds:
                    start, count = now, 0
                if count >= maximum:
                    raise HTTPException(429, "Too many attempts. Please try again later.", headers={"Retry-After": str(seconds)})
                self.buckets[key] = (start, count + 1)
                self.buckets.move_to_end(key)
            while len(self.buckets) > 2048:
                self.buckets.popitem(last=False)

    def _token(self, data):
        try:
            response = self.client.post(self.issuer + "/protocol/openid-connect/token",
                data={"client_id": self.client_id, "client_secret": self.secret, **data})
            if response.status_code == 400:
                raise HTTPException(401, "Sign-in failed. Check your details or ask your administrator to complete account setup.")
            if response.status_code in (401, 403):
                raise HTTPException(503, "Account service is unavailable")
            response.raise_for_status()
            result = response.json()
            access = result["access_token"]
            if not isinstance(access, str) or not 10 <= len(access) <= 65536:
                raise ValueError()
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "Account service is unavailable") from None

    def token_result(self, data):
        result = self._token(data)
        refresh = result.get("refresh_token")
        lifetime = result.get("expires_in")
        if not isinstance(refresh, str) or not 10 <= len(refresh) <= 65536 or not isinstance(lifetime, int) or not 30 <= lifetime <= 86400:
            raise HTTPException(503, "Account service returned an invalid session")
        return {"accessToken": result["access_token"], "refreshToken": refresh, "expiresIn": lifetime}

    def login(self, form):
        return self.token_result({"grant_type": "password", "username": form.username.strip(), "password": form.password.get_secret_value(), "scope": "openid profile"})

    def refresh(self, form):
        return self.token_result({"grant_type": "refresh_token", "refresh_token": form.refreshToken.get_secret_value()})

    def _management(self):
        result = self._token({"grant_type": "client_credentials", "scope": "kc-admin-api"})
        return {"Authorization": "Bearer " + result["access_token"]}

    def register(self, form):
        first, last = form.fullName.rsplit(" ", 1)
        attributes = {"phone_number": [form.phone.strip()], "requested_role": [form.role.value]}
        if self.pilot_org:
            attributes["dhis_org_unit"] = [self.pilot_org]
        try:
            response = self.client.post(self.admin + "/users", headers=self._management(), json={
                "username": form.email.strip().lower(), "email": form.email.strip().lower(),
                "firstName": first, "lastName": last, "enabled": True, "emailVerified": False,
                "attributes": attributes, "requiredActions": [],
                "credentials": [{"type": "password", "value": form.password.get_secret_value(), "temporary": False}],
            })
            if response.status_code == 409:
                raise HTTPException(409, "Account could not be created. If already registered, sign in or contact your administrator.")
            if response.status_code == 400:
                raise HTTPException(422, "Account details or password do not meet the configured account policy")
            response.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(503, "Account creation is temporarily unavailable") from None
        return {"status": "ENGINEERING_PILOT" if self.pilot_org else "AWAITING_ASSIGNMENT",
                "message": "Account created for engineering-pilot testing. Use test data only." if self.pilot_org else "Account created. Your administrator must assign your facility before you can sign in."}

    def reset(self, form):
        if not self.smtp:
            raise HTTPException(503, "Password reset email is not configured. Contact your programme administrator.")
        try:
            headers = self._management()
            response = self.client.get(self.admin + "/users", headers=headers, params={"email" if "@" in form.username else "username": form.username.strip(), "exact": "true"})
            response.raise_for_status()
            for user in response.json()[:1]:
                response = self.client.put(self.admin + "/users/" + user["id"] + "/execute-actions-email", headers=headers, json=["UPDATE_PASSWORD"])
                response.raise_for_status()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "Password reset is temporarily unavailable. Contact your programme administrator.") from None
        return {"message": "If the account exists and has a valid email address, password-reset instructions have been requested."}
