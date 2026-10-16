"""
collectors/admin/auth.py — the ops page is for one person. The check happens HERE, on the server.

Verifies a Firebase ID token (RS256, Google's rotating certs) for project scantoknow-a1593:
signature, audience, issuer, expiry, verified email, and that the email is on the allow-list.
The allow-list defaults to the owner and can be overridden with QS_ADMIN_EMAILS.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Any, Callable, Dict, Optional

import jwt
from cryptography import x509
from fastapi import HTTPException, Request

ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("QS_ADMIN_EMAILS", "rahultiwari1295@gmail.com").split(",") if e.strip()}
FIREBASE_PROJECT = os.environ.get("QS_FIREBASE_PROJECT", "scantoknow-a1593")
CERT_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"


class AuthError(Exception):
    pass


_cert_cache: Dict[str, Any] = {"certs": {}, "exp": 0.0}


def _fetch_certs() -> Dict[str, str]:
    import requests
    r = requests.get(CERT_URL, timeout=10)
    r.raise_for_status()
    age = 3600
    for part in r.headers.get("cache-control", "").split(","):
        part = part.strip()
        if part.startswith("max-age="):
            age = int(part.split("=")[1])
    _cert_cache.update(certs=r.json(), exp=time.time() + age)
    return _cert_cache["certs"]


def get_certs(force: bool = False) -> Dict[str, str]:
    if force or time.time() >= _cert_cache["exp"] or not _cert_cache["certs"]:
        return _fetch_certs()
    return _cert_cache["certs"]


def verify(token: str, certs_provider: Optional[Callable[[], Dict[str, str]]] = None,
           project: Optional[str] = None, allowed: Optional[set] = None) -> Dict[str, Any]:
    project = project or FIREBASE_PROJECT
    allowed = ADMIN_EMAILS if allowed is None else allowed
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise AuthError(f"malformed token: {e}")
    if header.get("alg") != "RS256":
        raise AuthError("bad algorithm")
    certs = (certs_provider or get_certs)()
    pem = certs.get(header.get("kid", ""))
    if pem is None and certs_provider is None:
        pem = get_certs(force=True).get(header.get("kid", ""))     # key rotated
    if pem is None:
        raise AuthError("unknown signing key")
    key = x509.load_pem_x509_certificate(pem.encode()).public_key()
    try:
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=project,
                            issuer=f"https://securetoken.google.com/{project}", leeway=10,
                            options={"require": ["exp", "iat", "sub", "aud", "iss"]})
    except jwt.PyJWTError as e:
        raise AuthError(f"invalid token: {e}")
    if not claims.get("sub"):
        raise AuthError("no subject")
    email = str(claims.get("email") or "").lower()
    if not email or not claims.get("email_verified"):
        raise AuthError("email not verified")
    if email not in allowed:
        raise AuthError("not an admin")
    claims["email"] = email
    return claims


_fails: Dict[str, deque] = defaultdict(deque)


def _throttle(ip: str) -> None:
    q = _fails[ip]
    now = time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= 20:
        raise HTTPException(429, "too many attempts")


def require_admin(request: Request) -> Dict[str, Any]:
    ip = request.client.host if request.client else "?"
    _throttle(ip)
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        _fails[ip].append(time.time())
        raise HTTPException(401, "sign in required")
    try:
        claims = verify(h[7:].strip())
    except AuthError as e:
        _fails[ip].append(time.time())
        raise HTTPException(403, "admin only") from e
    claims["_ip"] = ip
    return claims
