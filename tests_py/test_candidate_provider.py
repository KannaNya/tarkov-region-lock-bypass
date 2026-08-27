from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from tarkov_cis.cli import LiveCandidateProvider
from tarkov_cis.config import AppConfig
from tarkov_cis.models import FailureRecord, Relay


UTC = timezone.utc


def relay(*, verified_at=None) -> Relay:
    return Relay(
        host_name="vpn.example",
        ip="192.0.2.10",
        port=443,
        country_short="RU",
        score=100,
        ping=20,
        sessions=1,
        source="RecentKnownGood" if verified_at else "NativeCatalog",
        source_priority=0 if verified_at else 3,
        verified_at=verified_at,
    )


class CandidateProviderTests(unittest.TestCase):
    def make_provider(self, directory: str) -> LiveCandidateProvider:
        provider = LiveCandidateProvider(AppConfig(), state_dir=Path(directory))
        provider._https_relays = lambda: ()
        provider._native_relays = lambda: ()
        return provider

    def test_recent_known_good_survives_both_live_catalog_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = self.make_provider(directory)
            provider._known_good = [relay(verified_at=datetime.now(UTC))]
            selected = provider.candidates()
        self.assertEqual(("192.0.2.10:443",), tuple(item.endpoint for item in selected))
        self.assertEqual("RecentKnownGood", selected[0].source)

    def test_success_clears_endpoint_cooldown_and_persists_known_good(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = self.make_provider(directory)
            current = relay()
            provider._failures = [
                FailureRecord(current.endpoint, datetime.now(UTC), "offline")
            ]
            provider.record_success(current)

            restored = self.make_provider(directory)
            self.assertEqual([], restored._failures)
            self.assertEqual(1, len(restored._known_good))
            self.assertEqual(current.endpoint, restored._known_good[0].endpoint)
            self.assertEqual("RecentKnownGood", restored._known_good[0].source)

    def test_read_only_list_never_activates_all_cooling_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = self.make_provider(directory)
            current = relay()
            provider._native_relays = lambda: (current,)
            provider._failures = [
                FailureRecord(
                    current.endpoint,
                    datetime.now(UTC) - timedelta(minutes=3),
                    "offline",
                )
            ]
            self.assertEqual((), provider.list_candidates())
            self.assertEqual((current,), provider.candidates())


if __name__ == "__main__":
    unittest.main()
