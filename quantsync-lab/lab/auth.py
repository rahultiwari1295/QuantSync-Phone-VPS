from __future__ import annotations

import logging

from fastapi import Depends, Header, HTTPException, Request

log = logging.getLogger("qslab.auth")
_fb = {"ready": False}


def _init_firebase(project_id: str) -> None:
    if not _fb["ready"]:
        import firebase_admin
        from firebase_admin import credentials
        import google.auth.credentials

        class NoAuth(google.auth.credentials.AnonymousCredentials):
            pass

        class DummyCred(credentials.Base):
            def get_credential(self):
                return NoAuth()

        firebase_admin.initialize_app(
            credential=DummyCred(),
            options={"projectId": project_id} if project_id else None,
        )
        _fb["ready"] = True


def optional_uid(request: Request, authorization: str | None = Header(None), x_dev_uid: str | None = Header(None)) -> str | None:
    cfg = request.app.state.cfg
    if cfg.auth_mode == "dev":
        return x_dev_uid
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        from firebase_admin import auth
        _init_firebase(cfg.firebase_project_id)
        return auth.verify_id_token(authorization[7:])["uid"]
    except Exception as e:  # noqa: BLE001
        # Public pages (today, strategies, scans, charts) must keep working for anyone, so an unverifiable token is treated
        # as "not signed in". Endpoints that need a user answer 401 through require_uid.
        log.warning("id token rejected (%s: %s) - check firebase_project_id=%r and outbound access to Google certs",
                    type(e).__name__, str(e)[:160], cfg.firebase_project_id)
        return None


def require_uid(uid: str | None = Depends(optional_uid)) -> str:
    if not uid:
        raise HTTPException(401, "Sign in to continue.")
    return uid


def mirror_strategy_ids(cfg, uid: str, strategy_ids: list[str], follow_ids: list[str], handle: str | None) -> None:
    """Best-effort copy of the id lists to users/{uid}.lab (Admin SDK). The VPS database stays the source of truth."""
    if not cfg.firestore_mirror:
        return
    try:
        from firebase_admin import firestore
        _init_firebase(cfg.firebase_project_id)
        firestore.client().collection("users").document(uid).set(
            {"lab": {"handle": handle, "strategyIds": strategy_ids[:100], "followIds": follow_ids[:200]}}, merge=True)
    except Exception:  # noqa: BLE001
        log.warning("firestore mirror failed for %s", uid, exc_info=True)
