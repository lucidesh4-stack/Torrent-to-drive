from __future__ import annotations
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
import hmac
import hashlib
import base64
import json
import time
from ..config import settings

class SessionMiddleware(BaseHTTPMiddleware):
    """A simple signed-cookie session middleware mimicking Flask's session."""
    
    def __init__(self, app, secret_key: str, session_ttl: int = 86400):
        super().__init__(app)
        self.secret_key = secret_key.encode()
        self.session_ttl = session_ttl

    def _sign(self, value: str) -> str:
        return hmac.new(self.secret_key, value.encode(), hashlib.sha256).hexdigest()

    def _encode(self, data: dict) -> str:
        json_data = json.dumps(data)
        encoded = base64.b64encode(json_data.encode()).decode()
        signature = self._sign(encoded)
        return f"{encoded}.{signature}"

    def _decode(self, cookie_val: str) -> dict | None:
        try:
            encoded, signature = cookie_val.split(".")
            if self._sign(encoded) != signature:
                return None
            return json.loads(base64.b64decode(encoded).decode())
        except Exception:
            return None

    async def dispatch(self, request: Request, call_next):
        # Load session from cookie
        session_cookie = request.cookies.get("session")
        request.state.session = self._decode(session_cookie) if session_cookie else {}
        
        response = await call_next(request)
        
        # If session was modified, save it back to cookie
        # (In a real app, we'd track modifications; here we just save if exists)
        if hasattr(request.state, "session"):
            encoded_session = self._encode(request.state.session)
            response.set_cookie(
                key="session",
                value=encoded_session,
                httponly=True,
                samesite="Lax",
                secure=settings.app_env == "production",
                max_age=self.session_ttl
            )
        return response
