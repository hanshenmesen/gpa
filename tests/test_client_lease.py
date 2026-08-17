import unittest

from gpa.replay.client_lease import (
    client_connected,
    client_status,
    disconnect_client,
    latest_active_client,
    mark_client_seen,
)


class ClientLeaseTests(unittest.TestCase):
    def test_leases_are_isolated_and_latest_active_client_wins(self):
        clients = {}
        mark_client_seen(
            clients,
            client_id="studio",
            environment={"page": "studio"},
            now=10,
            seen_at="first",
            timeout=20,
        )
        mark_client_seen(
            clients,
            client_id="store",
            environment={"page": "store"},
            now=12,
            seen_at="second",
            timeout=20,
        )

        self.assertTrue(client_connected(clients, now=15, timeout=20, client_id="studio"))
        self.assertEqual(latest_active_client(clients, now=15, timeout=20)["id"], "store")
        self.assertEqual(client_status(clients, fallback={}, now=15, timeout=20)["active_client_count"], 2)

        disconnect_client(clients, "store")

        self.assertEqual(latest_active_client(clients, now=15, timeout=20)["id"], "studio")

    def test_mark_preserves_environment_and_prunes_long_expired_entries(self):
        clients = {
            "old": {"id": "old", "last_seen_monotonic": 1, "environment": {"page": "old"}},
            "studio": {"id": "studio", "last_seen_monotonic": 90, "environment": {"page": "studio"}},
        }

        entry = mark_client_seen(
            clients,
            client_id="studio",
            environment=None,
            now=100,
            seen_at="now",
            timeout=20,
        )

        self.assertNotIn("old", clients)
        self.assertEqual(entry["environment"], {"page": "studio"})


if __name__ == "__main__":
    unittest.main()
