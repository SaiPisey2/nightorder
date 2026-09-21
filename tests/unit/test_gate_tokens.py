import time

import pytest

from nightorder.api.security import TokenError, mint_gate_token, verify_gate_token


def test_roundtrip():
    token = mint_gate_token("gate-123", "approve")
    action = verify_gate_token(token)
    assert action.gate_id == "gate-123"
    assert action.decision == "approve"
    assert len(action.nonce) == 32


def test_tamper_rejected():
    token = mint_gate_token("gate-123", "approve")
    with pytest.raises(TokenError, match="signature|malformed"):
        verify_gate_token(token[:-4] + "0000")


def test_decision_swap_rejected():
    # Signing binds the decision — a reject token cannot approve.
    approve = mint_gate_token("g", "approve")
    reject = mint_gate_token("g", "reject")
    body_a, sig_a = approve.rsplit(".", 1)
    _, sig_r = reject.rsplit(".", 1)
    with pytest.raises(TokenError):
        verify_gate_token(body_a + "." + sig_r)


def test_expired(monkeypatch):
    token = mint_gate_token("g", "approve", ttl_seconds=1)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 10)
    with pytest.raises(TokenError, match="expired"):
        verify_gate_token(token)


def test_nonces_unique():
    t1, t2 = mint_gate_token("g", "approve"), mint_gate_token("g", "approve")
    assert verify_gate_token(t1).nonce != verify_gate_token(t2).nonce
