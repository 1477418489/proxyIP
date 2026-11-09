import pytest

from cfproxy.jobs import summarize
from cfproxy.probes import COUNTRY_CODES, normalize_country_code


@pytest.mark.parametrize("value,expected", [
    ("jp", "JP"), ("US", "US"), (" FR ", "FR"), ("HK", "HK"), ("TW", "TW"),
    ("XX", None), ("ZZ", None), ("T1", None), ("QQ", None), ("XK", None),
    ("JPN", None), ("日本", None), ("", None), (None, None), (12, None),
])
def test_country_code_normalization(value, expected):
    assert normalize_country_code(value) == expected


def test_country_codes_are_complete_and_unique():
    assert len(COUNTRY_CODES) == 249


def sample(country=None, *, ok=True, target="www.cloudflare.com", include_country=True):
    result = {"ok": ok, "total_ms": 100, "target": target, "exit_ip": "1.2.3.4"}
    if include_country:
        result["exit_country"] = country
    if not ok:
        result["error"] = "Example failure"
    return result


def test_multiple_targets_keep_all_observed_countries_and_unknown_count():
    samples = [
        sample("JP"), sample("US", target="speed.cloudflare.com"),
        sample(None), sample("JP"), sample("DE", ok=False),
    ]
    result = summarize(samples, 5, "local", "test-job")
    assert result["exit_countries"] == ["JP", "US"]
    assert result["country_unknown_samples"] == 1
    assert result["successes"] == 4
    assert result["status"] == "unstable"


def test_old_samples_have_unknown_countries_without_changing_score():
    old = [sample(include_country=False), sample(include_country=False)]
    result = summarize(old, 2, "worker", "old-job")
    new = summarize([sample("JP"), sample("US")], 2, "worker", "new-job")
    assert result["exit_countries"] == []
    assert result["country_unknown_samples"] == 2
    assert result["status"] == "passed"
    assert result["score"] == new["score"]


def test_failed_samples_do_not_contribute_country_or_unknown_count():
    result = summarize([sample("JP", ok=False), sample(None, ok=False)], 2, "local", "test-job")
    assert result["exit_countries"] == []
    assert result["country_unknown_samples"] == 0


def test_empty_summary_has_no_observed_country():
    result = summarize([], 2, "local", "test-job", "running")
    assert result["exit_countries"] == []
    assert result["country_unknown_samples"] == 0
    assert result["score"] is None
