"""Signed, single-use gate action tokens.

Token = base64url(payload) . hex(HMAC-SHA256(secret, payload))
payload = gate_id|decision|nonce|expiry-epoch

Verification checks signature, expiry, then burns the nonce in the DB
(single-use / replay protection). Tokens are minted only by the kernel —
never by a delivery channel or an LLM.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from bosun.config import settings


class TokenError(Exception):
    pass


@dataclass
class GateAction:
    gate_id: str
    decision: str  # approve | reject
    nonce: str


def _sign(payload: bytes) -> str:
    return hmac.new(settings().gate_token_secret.encode(), payload, hashlib.sha256).hexdigest()


def mint_gate_token(gate_id: str, decision: str, ttl_seconds: int | None = None) -> str:
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve|reject")
    ttl = ttl_seconds if ttl_seconds is not None else settings().gate_token_ttl_seconds
    expiry = int(time.time()) + ttl
    payload = f"{gate_id}|{decision}|{secrets.token_hex(16)}|{expiry}".encode()
    return base64.urlsafe_b64encode(payload).decode() + "." + _sign(payload)


def verify_gate_token(token: str) -> GateAction:
    """Signature + expiry check only — nonce burning happens at the DB layer."""
    try:
        encoded, signature = token.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(encoded.encode())
    except Exception as e:
        raise TokenError("malformed token") from e
    if not hmac.compare_digest(_sign(payload), signature):
        raise TokenError("bad signature")
    try:
        gate_id, decision, nonce, expiry = payload.decode().split("|")
    except ValueError as e:
        raise TokenError("malformed payload") from e
    if int(expiry) < time.time():
        raise TokenError("token expired")
    return GateAction(gate_id=gate_id, decision=decision, nonce=nonce)
