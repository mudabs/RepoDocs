"""URL redaction and normalization never retain userinfo, queries, or fragments."""

import pytest

from repodocs.correlation import paths_match
from repodocs.urls import safe_url, url_target


@pytest.mark.parametrize("url, expected", [
    ("https://user:pass@EXAMPLE.com:443/a?token=secret#private", "https://example.com/a"),
    ("/api/orders?api_key=secret&name=private", "/api/orders"),
    ("https://[::1]:8080/api", "https://[::1]:8080/api"),
    ("javascript:alert(1)", None),
    ("https://example.invalid:bad/api", None),
    ("https://{host}/api", None),
    ("https://example.invalid\\evil", None),
])
def test_safe_url(url, expected) -> None:
    assert safe_url(url) == expected


def test_base_paths_and_absolute_overrides() -> None:
    assert url_target("/orders", "https://shop.invalid/api/") == ("https://shop.invalid", "/api/orders")
    assert url_target("https://other.invalid/orders", "https://shop.invalid/api") == ("https://other.invalid", "/orders")
    assert url_target("/orders", "/api") == (None, "/api/orders")


@pytest.mark.parametrize("left, right, match", [
    ("/orders/1", "/orders/{id}", True),
    ("/orders/{id}", "/orders/{orderId}", True),
    ("/orders/{id}", "/orders/{id}/items", False),
    ("/orders/.*", "/orders/1", False),
    ("/Orders", "/orders", False),
    ("/orders", "/orders/", False),
    ("/orders/{id}", "/orders/", False),
    ("/orders/1", "/orders/{id:[0-9]+}", False),
    ("/orders/{id}", "/orders/*", False),
    ("/orders/x1", "/orders/x{id}", False),
])
def test_paths_match_without_regex_or_runtime_evaluation(left, right, match) -> None:
    assert paths_match(left, right) == match
