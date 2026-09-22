"""Gate delivery URLs come from the pipeline spec, and the worker runs inside
the cluster, so an unchecked URL turns it into a request proxy."""
import pytest

from nightorder.net import BlockedURL, check_outbound_url


@pytest.fixture(autouse=True)
def no_local_override(monkeypatch):
    monkeypatch.delenv("NIGHTORDER_ALLOW_LOCAL_WEBHOOKS", raising=False)


# --- refused ---------------------------------------------------------------

def test_cloud_metadata_address_is_refused():
    """169.254.169.254 hands out credentials to anything on the right network."""
    with pytest.raises(BlockedURL) as e:
        check_outbound_url("http://169.254.169.254/latest/meta-data/iam/security-credentials/")
    assert "link-local" in str(e.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata/computeMetadata/v1/",
        "http://metadata.goog/",
        "http://instance-data/latest/",
        "http://METADATA.GOOGLE.INTERNAL/",   # case
        "http://metadata.google.internal./",  # trailing dot
    ],
)
def test_metadata_hostnames_are_refused_by_name(url):
    with pytest.raises(BlockedURL) as e:
        check_outbound_url(url)
    assert "metadata service" in str(e.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8400/gates",
        "http://localhost:8400/gates",
        "http://[::1]:8400/gates",
    ],
)
def test_loopback_is_refused(url):
    with pytest.raises(BlockedURL):
        check_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/",
        "//example.com/no-scheme",
    ],
)
def test_non_http_schemes_are_refused(url):
    with pytest.raises(BlockedURL):
        check_outbound_url(url)


def test_url_without_a_host_is_refused():
    with pytest.raises(BlockedURL):
        check_outbound_url("https://")


def test_unresolvable_host_is_refused():
    with pytest.raises(BlockedURL):
        check_outbound_url("https://this-name-should-not-resolve.invalid/hook")


def test_multicast_is_refused():
    with pytest.raises(BlockedURL):
        check_outbound_url("http://224.0.0.1/hook")


# --- allowed ---------------------------------------------------------------

def test_a_private_address_is_allowed_because_self_hosted_relays_are_normal():
    check_outbound_url("https://10.1.2.3/hook")
    check_outbound_url("https://192.168.1.10/hook")


def test_loopback_is_allowed_when_explicitly_opted_in(monkeypatch):
    monkeypatch.setenv("NIGHTORDER_ALLOW_LOCAL_WEBHOOKS", "1")
    check_outbound_url("http://127.0.0.1:8400/gates")


def test_link_local_is_still_refused_by_name_even_when_local_is_allowed(monkeypatch):
    """The hostname rule is not an address rule, so the opt-out must not
    re-open the metadata service."""
    monkeypatch.setenv("NIGHTORDER_ALLOW_LOCAL_WEBHOOKS", "1")
    with pytest.raises(BlockedURL):
        check_outbound_url("http://metadata.google.internal/")
