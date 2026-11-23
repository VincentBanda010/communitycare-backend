import httpx
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .config import UID, https_url

bearer = HTTPBearer(auto_error=False)


class TokenVerifier:
    def __init__(self, settings):
        self.issuer = settings.issuer
        self.audience = settings.audience
        https_url(self.issuer)
        response = httpx.get(
            self.issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=10,
        )
        response.raise_for_status()
        metadata = response.json()
        if metadata.get("issuer") != self.issuer:
            raise ValueError("OIDC discovery issuer does not match OIDC_ISSUER")
        self.keys = jwt.PyJWKClient(https_url(metadata["jwks_uri"]), timeout=10)

    def __call__(self, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
        try:
            key = self.keys.get_signing_key_from_jwt(credentials.credentials)
            claims = jwt.decode(
                credentials.credentials, key.key, algorithms=["RS256"],
                issuer=self.issuer, audience=self.audience,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError:
            raise HTTPException(401, "Invalid access token", headers={"WWW-Authenticate": "Bearer"}) from None
        return claims


def org_unit(claims):
    value = claims.get("dhis_org_unit")
    if not isinstance(value, str) or not UID.fullmatch(value):
        raise HTTPException(403, "A valid dhis_org_unit claim is required")
    return value
