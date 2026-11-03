from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from ..monitor import IncompleteMatch, Monitor, Store, build_report
from ..pubg_api import PubgError


def player(account="account.a", name="NightHunter", matches=()):
    return {"id": account, "attributes": {"name": name},
            "relationships": {"matches": {"data": [{"id": mid} for mid in matches]}}}


def fixture(mid="new-win", age=60):
    included = []
    for i, (aid, name, damage, kills) in enumerate([
        ("account.a", "NightHunter", 1568.2, 12),
        ("account.b", "CloudRunner", 842.4, 6),
        ("account.c", "LuckyPotato", 563, 4),
        ("account.d", "JustOneMore", 311, 2),
        ("account.loser", "OtherTeam", 140, 1),
    ]):
        included.append({"type": "participant", "id": f"p{i}", "attributes": {"stats": {
            "playerId": aid, "name": name, "winPlace": 2 if i == 4 else 1,
            "kills": kills, "damageDealt": damage, "assists": 3, "DBNOs": 2,
            "headshotKills": 1, "revives": 1, "longestKill": 210.2,
        }}})
    included.append({"type": "roster", "id": "winners", "attributes": {"stats": {"rank": 1}},
                     "relationships": {"participants": {"data": [
                         {"type": "participant", "id": f"p{i}"} for i in range(4)]}}})
    included.append({"type": "roster", "id": "losers", "attributes": {"stats": {"rank": 2}},
                     "relationships": {"participants": {"data": [{"type": "participant", "id": "p4"}]}}})
    return {"data": {"id": mid, "attributes": {
        "gameMode": "squad-fpp", "mapName": "Baltic_Main", "duration": 1902,
        "isCustomMatch": False, "matchType": "official",
        "createdAt": datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat(),
    }}, "included": included}


class ReportTests(unittest.TestCase):
    def test_full_roster_contains_unmonitored_teammates_and_real_stats(self):
        result = build_report(fixture(), {"account.b"})
        self.assertEqual(len(result.players), 4)
        self.assertEqual(sum(p.kills for p in result.players), 24)
        self.assertAlmostEqual(sum(p.damage for p in result.players), 3284.6)
        self.assertEqual([p.name for p in result.players if p.monitored], ["CloudRunner"])
        self.assertEqual(result.players[0].headshots, 1)
        self.assertEqual(result.duration, 1902)

    def test_losing_player_does_not_trigger_winner_report(self):
        self.assertIsNone(build_report(fixture(), {"account.loser"}))

    def test_missing_teammate_is_retried_not_silently_omitted(self):
        match = fixture()
        match["included"] = [p for p in match["included"] if p["id"] != "p3"]
        with self.assertRaises(IncompleteMatch):
            build_report(match, {"account.a"})

    def test_missing_roster_is_retried(self):
        match = fixture()
        match["included"] = [p for p in match["included"] if p["type"] != "roster"]
        with self.assertRaises(IncompleteMatch):
            build_report(match, {"account.a"})

    def test_missing_placement_is_retried_instead_of_recording_a_loss(self):
        match = fixture()
        del match["included"][0]["attributes"]["stats"]["winPlace"]
        with self.assertRaises(IncompleteMatch):
            build_report(match, {"account.a"})

    def test_modes_and_custom_games(self):
        match = fixture()
        match["data"]["attributes"]["isCustomMatch"] = True
        self.assertIsNone(build_report(match, {"account.a"}))
        self.assertIsNotNone(build_report(match, {"account.a"}, True))
        match["data"]["attributes"]["gameMode"] = "tdm"
        self.assertIsNone(build_report(match, {"account.a"}, True))

    def test_solo_and_duo_names(self):
        for mode, expected in [("solo", "单排 · TPP"), ("duo-fpp", "双排 · FPP"),
                               ("normal-squad-fpp", "四排 · FPP")]:
            match = fixture()
            match["data"]["attributes"]["gameMode"] = mode
            self.assertEqual(build_report(match, {"account.a"}).mode, expected)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "monitor.sqlite3"
        self.store = Store(self.db_path)
        self.sent = []
        self.client = AsyncMock()
        self.client.players.return_value = [player(matches=("old", "new-win"))]
        self.client.match.return_value = fixture()
        self.monitor = Monitor(self.client, self.store, self.send)
        self.origin = "napcat:GroupMessage:12345"

    async def send(self, origin, report):
        self.sent.append((origin, report))

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_first_subscription_skips_history_and_repeat_add_does_not_rebaseline(self):
        self.assertTrue(self.store.subscribe(self.origin, player(matches=("old",))))
        self.assertFalse(self.store.subscribe(self.origin, player(matches=("old", "new-win"))))
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.client.match.assert_awaited_once_with("new-win")

    async def test_two_monitored_teammates_produce_one_report(self):
        self.store.subscribe(self.origin, player(matches=("old",)))
        self.store.subscribe(self.origin, player("account.b", "CloudRunner"))
        self.client.players.return_value.append(player("account.b", "CloudRunner", ("new-win",)))
        await self.monitor.poll()
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(sum(p.monitored for p in self.sent[0][1].players), 2)

    async def test_teammate_with_delayed_player_snapshot_still_appears_in_shared_report(self):
        self.store.subscribe(self.origin, player(matches=("old",)))
        self.store.subscribe(self.origin, player("account.b", "CloudRunner"))
        self.client.players.return_value.append(player("account.b", "CloudRunner"))
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(sum(p.monitored for p in self.sent[0][1].players), 2)

    async def test_same_match_in_two_groups_is_sent_to_both(self):
        self.store.subscribe(self.origin, player(matches=("old",)))
        self.store.subscribe("napcat:GroupMessage:67890", player(matches=("old",)))
        await self.monitor.poll()
        self.assertEqual({o for o, _ in self.sent}, {self.origin, "napcat:GroupMessage:67890"})

    async def test_restart_does_not_repeat_sent_matches(self):
        self.store.subscribe(self.origin, player(matches=("old",)))
        await self.monitor.poll()
        self.store.close()
        self.store = Store(self.db_path)
        self.monitor = Monitor(self.client, self.store, self.send)
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)

    async def test_restart_recovers_pending_report(self):
        self.store.subscribe(self.origin, player())
        await self.monitor.process_match(fixture(), {"account.a"})
        self.store.close()
        self.store = Store(self.db_path)
        self.monitor = Monitor(self.client, self.store, self.send)
        await self.monitor.flush()
        self.assertEqual(len(self.sent), 1)

    async def test_failed_send_is_retained_and_retried(self):
        self.store.subscribe(self.origin, player())
        await self.monitor.process_match(fixture(), {"account.a"})
        self.monitor.send_report = AsyncMock(side_effect=RuntimeError("offline"))
        await self.monitor.flush()
        self.assertEqual(len(self.sent), 0)
        row = self.store.db.execute("SELECT * FROM outbox").fetchone()
        self.assertIsNone(row["sent_at"])
        self.assertEqual(row["attempts"], 1)
        self.assertGreater(row["next_try"], time.time())
        self.store.db.execute("UPDATE outbox SET next_try=0")
        self.store.db.commit()
        self.monitor.send_report = self.send
        await self.monitor.flush()
        self.assertEqual(len(self.sent), 1)

    async def test_incomplete_match_is_not_marked_seen(self):
        self.store.subscribe(self.origin, player())
        match = fixture()
        match["included"] = [p for p in match["included"] if p["id"] != "p3"]
        with self.assertRaises(IncompleteMatch):
            await self.monitor.process_match(match, {"account.a"})
        self.assertFalse(self.store.seen(self.store.subscriptions()[0], "new-win"))
        await self.monitor.process_match(fixture(), {"account.a"})
        await self.monitor.flush()
        self.assertEqual(len(self.sent), 1)

    async def test_missing_watched_participant_is_retried(self):
        self.store.subscribe(self.origin, player())
        match = fixture()
        match["included"] = [p for p in match["included"] if p["id"] != "p0"]
        with self.assertRaises(IncompleteMatch):
            await self.monitor.process_match(match, {"account.a"})

    async def test_old_matches_are_skipped(self):
        self.store.subscribe(self.origin, player())
        await self.monitor.process_match(fixture(age=7 * 3600), {"account.a"})
        await self.monitor.flush()
        self.assertEqual(self.sent, [])
        self.assertTrue(self.store.seen(self.store.subscriptions()[0], "new-win"))

    async def test_removing_winner_cancels_pending_even_when_loser_is_subscribed(self):
        self.store.subscribe(self.origin, player())
        self.store.subscribe(self.origin, player("account.loser", "OtherTeam"))
        await self.monitor.process_match(fixture(), {"account.a"})
        self.assertEqual(self.store.remove(self.origin, "NightHunter"), 1)
        await self.monitor.flush()
        self.assertEqual(self.sent, [])

    async def test_rename_keeps_stable_account_and_original_removal_name(self):
        self.store.subscribe(self.origin, player(matches=("old",)))
        self.client.players.return_value[0]["attributes"]["name"] = "RenamedPlayer"
        await self.monitor.poll()
        self.assertEqual(self.store.subscriptions()[0]["name"], "RenamedPlayer")
        self.assertEqual(self.store.remove(self.origin, "NightHunter"), 1)

    async def test_network_failure_does_not_reset_baseline(self):
        self.store.subscribe(self.origin, player(matches=("old",)))
        self.client.players.side_effect = PubgError("暂时离线")
        await self.monitor.poll()
        self.client.players.side_effect = None
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)

    async def test_group_whitelist_supports_bot_prefixed_sessions(self):
        monitor = Monitor(self.client, self.store, self.send, allowed_groups=["12345"])
        self.assertTrue(monitor.allowed("napcat:GroupMessage:2622811802_12345"))
        self.assertFalse(monitor.allowed("napcat:GroupMessage:67890"))

    async def test_bad_baseline_is_not_saved(self):
        bad = player()
        del bad["relationships"]
        with self.assertRaises(PubgError):
            self.store.subscribe(self.origin, bad)
        self.assertEqual(self.store.subscriptions(), [])
