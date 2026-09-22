"""Captured output is persisted, shown in the UI, and sent to the advisory
model, so anything this misses leaves the machine."""
import pytest

from nightorder.redaction import REDACTED, redact

SECRET = "hunter2secret"


def _hidden(text: str, secret: str) -> bool:
    return secret not in redact(text)


# --- labelled secrets ------------------------------------------------------

@pytest.mark.parametrize(
    "line, secret",
    [
        ("api_key=sk-ant-abc12345678", "sk-ant-abc12345678"),
        ("API_KEY: sk-ant-abc12345678", "sk-ant-abc12345678"),
        ("apikey=abcdef123456", "abcdef123456"),
        (f"password: {SECRET}", SECRET),
        (f"passwd={SECRET}", SECRET),
        (f"pwd = {SECRET}", SECRET),
        (f"secret={SECRET}", SECRET),
        (f"credential: {SECRET}", SECRET),
        (f"access_key={SECRET}", SECRET),
        (f"private_key={SECRET}", SECRET),
        (f'token = "{SECRET}"', SECRET),
        (f"export SECRET='my super long {SECRET}'", SECRET),
        (f'password: "value with spaces {SECRET}"', SECRET),
    ],
)
def test_labelled_secrets_are_hidden(line, secret):
    assert _hidden(line, secret)


# --- the format that the original regex missed -----------------------------

@pytest.mark.parametrize(
    "line, secret",
    [
        (
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
            "eyJhbGciOiJIUzI1NiJ9.payload.signature",
        ),
        ("authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("auth=Token abcdefghijklmnopqrst", "abcdefghijklmnopqrst"),
        (
            "curl -H 'Authorization: Bearer abcdefghijklmnop'",
            "abcdefghijklmnop",
        ),
        ("Bearer abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
    ],
)
def test_scheme_prefixed_secrets_are_hidden(line, secret):
    """`Authorization: Bearer <token>` is the commonest secret in a log and the
    original pattern let it through: the value sits behind the scheme word."""
    assert _hidden(line, secret)


@pytest.mark.parametrize(
    "line, secret",
    [
        ("Authorization: Bearer short123", "short123"),
        ("auth=Token abcdefghij", "abcdefghij"),
        ("authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ],
)
def test_short_scheme_values_are_hidden(line, secret):
    """Values below the unlabelled scheme rule's length floor, and not JWT- or
    provider-shaped, are caught only by the labelled-scheme rule. Without these
    cases that rule is not pinned by any test — every other scheme case here is
    also matched by the jwt or provider-token rule."""
    assert _hidden(line, secret)


def test_the_scheme_word_itself_is_kept_for_readability():
    assert redact("Authorization: Bearer abcdefghijklmnop") == f"Authorization: Bearer {REDACTED}"


# --- unlabelled, recognised by shape ---------------------------------------

@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-loose-key-with-no-label",
        "sk-abcdefghijklmnopqrstuvwxyz12",
        "ghp_0123456789abcdefghij",
        "gho_0123456789abcdefghij",
        "github_pat_0123456789abcdefghij_more",
        "xoxb-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAIOSFODNN7EXAMPLE",
        "AIzaSyD-abcdefghijklmnopqrstuvwxyz1234567",
        "glpat-abcdefghijklmnopqrst",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    ],
)
def test_shaped_tokens_are_hidden_without_any_label(secret):
    assert _hidden(f"worker log line containing {secret} mid-sentence", secret)


def test_webhook_urls_are_hidden_because_the_path_is_the_credential():
    for url in [
        "https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXX",
        "https://example.webhook.office.com/webhookb2/abc-def/IncomingWebhook/123",
        "https://discord.com/api/webhooks/123456/abcdef",
    ]:
        assert redact(f"posting to {url} now") == f"posting to {REDACTED} now"


def test_password_in_a_connection_string_is_hidden():
    assert _hidden("postgresql://user:hunter2@db:5432/app", "hunter2")
    assert "user" in redact("postgresql://user:hunter2@db:5432/app")


def test_private_key_block_is_hidden_entirely():
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIabcdef\nMIIghijkl\n-----END RSA PRIVATE KEY-----"
    assert redact(f"before\n{block}\nafter") == f"before\n{REDACTED}\nafter"


# --- it must not eat ordinary output ---------------------------------------

@pytest.mark.parametrize(
    "line",
    [
        "ok=fine",
        "step finished in 4.2s with exit code 0",
        "submitted workflow nightorder-a1b2c3d4e5f6",
        "INFO  resolving parameter yearmonth",
        "GET /runs/abc HTTP/1.1 200",
        "https://example.com/docs/getting-started",
        "region=us_en partition=20260921",
    ],
)
def test_ordinary_output_is_untouched(line):
    assert redact(line) == line


# --- input handling --------------------------------------------------------

@pytest.mark.parametrize("value", ["", None, 0])
def test_empty_and_non_string_input_is_returned_unchanged(value):
    assert redact(value) == value


def test_multiple_secrets_on_one_line_are_all_hidden():
    line = "api_key=sk-ant-abc12345678 and Authorization: Bearer abcdefghijklmnop"
    out = redact(line)
    assert "sk-ant-abc12345678" not in out and "abcdefghijklmnop" not in out
