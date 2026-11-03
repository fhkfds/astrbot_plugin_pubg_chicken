"""Persistent subscriptions and a transactional notification outbox."""
import asyncio
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
import time

from .pubg_api import PubgError, player_matches
from .render_card import Player, Victory

LOG = logging.getLogger("astrbot.pubg_chicken")
BR_MODES = {p + mode + suffix for p in ("", "normal-")
            for mode in ("solo", "duo", "squad") for suffix in ("", "-fpp")}
MAPS = {
    "Baltic_Main": "艾伦格 / ERANGEL", "Desert_Main": "米拉玛 / MIRAMAR",
    "Savage_Main": "萨诺 / SANHOK", "DihorOtok_Main": "维寒迪 / VIKENDI",
    "Tiger_Main": "泰戈 / TAEGO", "Kiki_Main": "帝斯顿 / DESTON",
    "Neon_Main": "荣都 / RONDO", "Summerland_Main": "卡拉金 / KARAKIN",
    "Chimera_Main": "帕拉莫 / PARAMO", "Heaven_Main": "褐湾 / HAVEN",
}


class IncompleteMatch(ValueError):
    pass


def match_timestamp(match):
    try:
        value = match["data"]["attributes"]["createdAt"]
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.timestamp()
    except (KeyError, TypeError, ValueError):
        raise IncompleteMatch("对局时间缺失") from None


def build_report(match: dict, monitored: set[str], include_custom=False) -> Victory | None:
    """Follow roster references, including teammates who aren't subscribed."""
    try:
        attrs = match["data"]["attributes"]
        mode = attrs["gameMode"]
        if mode not in BR_MODES:
            return None
        if not include_custom and (attrs.get("isCustomMatch") or attrs.get("matchType") == "custom"):
            return None
        included = match["included"]
        participants = {p["id"]: p["attributes"]["stats"] for p in included
                        if p["type"] == "participant"}
        for stats in participants.values():
            if stats.get("playerId") in monitored:
                if not isinstance(stats.get("winPlace"), int) or stats["winPlace"] < 1:
                    raise IncompleteMatch("受监控玩家的最终名次尚未完整")
        watched_winners = {pid for pid, stats in participants.items()
                           if stats.get("playerId") in monitored and stats.get("winPlace") == 1}
        if not watched_winners:
            return None
        for roster in included:
            if roster["type"] != "roster":
                continue
            refs = [p["id"] for p in roster["relationships"]["participants"]["data"]]
            if not watched_winners.intersection(refs):
                continue
            if not refs or any(pid not in participants for pid in refs):
                raise IncompleteMatch("冠军队伍的队员数据尚未完整")
            stats = [participants[pid] for pid in refs]
            if any(p.get("winPlace") != 1 for p in stats):
                raise IncompleteMatch("冠军队伍名次不一致")
            players = tuple(Player(
                name=p["name"], kills=int(p["kills"]), damage=float(p["damageDealt"]),
                assists=int(p["assists"]), knocks=int(p["DBNOs"]),
                headshots=int(p.get("headshotKills", 0)), revives=int(p.get("revives", 0)),
                longest_kill=float(p.get("longestKill", 0)),
                monitored=p.get("playerId") in monitored,
            ) for p in sorted(stats, key=lambda p: (-p["damageDealt"], -p["kills"], p["name"])))
            stamp = datetime.fromtimestamp(match_timestamp(match), timezone(timedelta(hours=8)))
            base_mode = mode.removeprefix("normal-").removesuffix("-fpp")
            friendly_mode = {"solo": "单排", "duo": "双排", "squad": "四排"}[base_mode]
            return Victory(
                players=players, map_name=MAPS.get(attrs["mapName"], attrs["mapName"]),
                mode=friendly_mode + (" · FPP" if mode.endswith("-fpp") else " · TPP"),
                match_time=stamp.strftime("%Y.%m.%d  %H:%M"), duration=int(attrs["duration"]),
                match_id=match["data"]["id"],
            )
        raise IncompleteMatch("冠军队伍信息尚未完整")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, IncompleteMatch):
            raise
        raise IncompleteMatch("对局数据不完整") from None


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                origin TEXT NOT NULL, account_id TEXT NOT NULL, name TEXT NOT NULL,
                original_name TEXT NOT NULL, subscribed_at REAL NOT NULL,
                PRIMARY KEY(origin, account_id));
            CREATE TABLE IF NOT EXISTS seen (
                origin TEXT NOT NULL, account_id TEXT NOT NULL, match_id TEXT NOT NULL,
                created_at REAL NOT NULL, PRIMARY KEY(origin, account_id, match_id),
                FOREIGN KEY(origin, account_id) REFERENCES subscriptions(origin, account_id)
                    ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS outbox (
                origin TEXT NOT NULL, match_id TEXT NOT NULL, payload TEXT NOT NULL,
                watched TEXT NOT NULL, created_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, sent_at REAL,
                PRIMARY KEY(origin, match_id));
        """)

    def subscriptions(self, origin=None):
        if origin is None:
            return list(self.db.execute("SELECT * FROM subscriptions ORDER BY origin, name"))
        return list(self.db.execute("SELECT * FROM subscriptions WHERE origin=? ORDER BY name", (origin,)))

    def subscribe(self, origin, player):
        matches = player_matches(player)
        account_id, name = player["id"], player["attributes"]["name"]
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO subscriptions VALUES (?, ?, ?, ?, ?)",
                (origin, account_id, name, name, time.time()),
            )
            if not cursor.rowcount:
                return False
            self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?)",
                                [(origin, account_id, mid, time.time()) for mid in matches])
        return True

    def remove(self, origin, name):
        selected = [s for s in self.subscriptions(origin) if name.casefold() in
                    (s["name"].casefold(), s["original_name"].casefold(), s["account_id"].casefold())]
        with self.db:
            for sub in selected:
                self.db.execute("DELETE FROM subscriptions WHERE origin=? AND account_id=?",
                                (origin, sub["account_id"]))
            remaining = {s["account_id"] for s in self.subscriptions(origin)}
            for row in self.db.execute("SELECT * FROM outbox WHERE origin=? AND sent_at IS NULL", (origin,)):
                if not remaining.intersection(json.loads(row["watched"])):
                    self.db.execute("DELETE FROM outbox WHERE origin=? AND match_id=?",
                                    (origin, row["match_id"]))
        return len(selected)

    def seen(self, sub, mid):
        return self.db.execute("SELECT 1 FROM seen WHERE origin=? AND account_id=? AND match_id=?",
                               (sub["origin"], sub["account_id"], mid)).fetchone() is not None

    def record(self, mid, subscriptions, reports):
        # Persist completion and outgoing reports together, before any network send.
        with self.db:
            for sub in subscriptions:
                self.db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?)",
                                (sub["origin"], sub["account_id"], mid, time.time()))
            for origin, report, watched in reports:
                self.db.execute("INSERT OR IGNORE INTO outbox(origin, match_id, payload, watched, created_at) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (origin, mid, json.dumps(asdict(report), ensure_ascii=False),
                                 json.dumps(sorted(watched)), time.time()))

    def pending(self, now):
        return list(self.db.execute("SELECT * FROM outbox WHERE sent_at IS NULL AND next_try<=? "
                                    "ORDER BY created_at LIMIT 20", (now,)))

    def delivered(self, row):
        with self.db:
            self.db.execute("UPDATE outbox SET sent_at=? WHERE origin=? AND match_id=?",
                            (time.time(), row["origin"], row["match_id"]))

    def failed(self, row):
        delay = min(1800, 30 * 2 ** min(row["attempts"], 6))
        with self.db:
            self.db.execute("UPDATE outbox SET attempts=attempts+1, next_try=? WHERE origin=? AND match_id=?",
                            (time.time() + delay, row["origin"], row["match_id"]))

    def cleanup(self):
        with self.db:
            self.db.execute("DELETE FROM seen WHERE created_at<?", (time.time() - 16 * 86400,))
            self.db.execute("DELETE FROM outbox WHERE sent_at IS NOT NULL AND sent_at<?",
                            (time.time() - 30 * 86400,))

    def close(self):
        self.db.close()


class Monitor:
    def __init__(self, client, store, send_report, *, include_custom=False, max_age_hours=6,
                 allowed_groups=()):
        self.client, self.store, self.send_report = client, store, send_report
        self.include_custom, self.max_age = include_custom, max_age_hours * 3600
        self.allowed_groups = {str(g) for g in allowed_groups}
        self.last_poll = 0.0
        self.last_error = ""
        self.match_retry = {}
        self._poll_lock = asyncio.Lock()

    def allowed(self, origin):
        group_id = origin.rsplit(":", 1)[-1].rsplit("_", 1)[-1]
        return not self.allowed_groups or group_id in self.allowed_groups

    async def poll(self):
        async with self._poll_lock:
            self.last_error = ""
            subs = [s for s in self.store.subscriptions() if self.allowed(s["origin"])]
            accounts = sorted({s["account_id"] for s in subs})
            candidates = defaultdict(set)
            for offset in range(0, len(accounts), 10):
                try:
                    players = await self.client.players(accounts[offset:offset + 10])
                    for player in players:
                        mids = player_matches(player)
                        aid = player["id"]
                        with self.store.db:
                            self.store.db.execute("UPDATE subscriptions SET name=? WHERE account_id=?",
                                                  (player["attributes"]["name"], aid))
                        for sub in self.store.subscriptions():
                            if sub["account_id"] == aid and self.allowed(sub["origin"]):
                                for mid in mids:
                                    if not self.store.seen(sub, mid):
                                        candidates[mid].add(aid)
                except PubgError as exc:
                    self.last_error = str(exc)
                    LOG.warning("PUBG 玩家查询失败：%s", exc)
            # Sorted IDs are only used for deterministic processing; no chronological assumption.
            for mid in sorted(candidates):
                if self.match_retry.get(mid, (0, 0))[1] > time.time():
                    continue
                try:
                    match = await self.client.match(mid)
                    if match["data"]["id"] != mid:
                        raise IncompleteMatch("对局 ID 不一致")
                    await self.process_match(match, candidates[mid])
                    self.match_retry.pop(mid, None)
                except (PubgError, IncompleteMatch, KeyError, TypeError) as exc:
                    attempts = self.match_retry.get(mid, (0, 0))[0] + 1
                    self.match_retry[mid] = attempts, time.time() + min(600, 30 * 2 ** min(attempts, 4))
                    self.last_error = str(exc)
                    LOG.warning("PUBG 对局暂未处理，稍后重试（%s）：%s", mid, exc)
            await self.flush()
            self.last_poll = time.time()
            self.store.cleanup()
            active = set(candidates)
            self.match_retry = {mid: item for mid, item in self.match_retry.items() if mid in active}

    async def process_match(self, match, referenced_accounts):
        mid = match["data"]["id"]
        age = time.time() - match_timestamp(match)
        subs = [s for s in self.store.subscriptions() if self.allowed(s["origin"])
                and not self.store.seen(s, mid)]
        if age > self.max_age:
            self.store.record(mid, [s for s in subs if s["account_id"] in referenced_accounts], [])
            return
        included = match.get("included", [])
        participants = {p["attributes"]["stats"].get("playerId") for p in included
                        if p.get("type") == "participant"}
        if not referenced_accounts.issubset(participants):
            raise IncompleteMatch("受监控玩家尚未出现在完整对局数据中")
        grouped = defaultdict(set)
        participating = [s for s in subs if s["account_id"] in participants]
        for sub in participating:
            grouped[sub["origin"]].add(sub["account_id"])
        reports = []
        for origin, monitored in grouped.items():
            report = build_report(match, monitored, self.include_custom)
            if report:
                winning_ids = {p["attributes"]["stats"].get("playerId") for p in included
                               if p.get("type") == "participant"
                               and p["attributes"]["stats"].get("winPlace") == 1}
                reports.append((origin, report, monitored.intersection(winning_ids)))
        self.store.record(mid, participating, reports)

    async def flush(self):
        for row in self.store.pending(time.time()):
            if not self.allowed(row["origin"]):
                continue
            if time.time() - row["created_at"] > self.max_age:
                # Expire rather than flood a group with stale notifications.
                self.store.delivered(row)
                continue
            try:
                payload = json.loads(row["payload"])
                payload["players"] = tuple(Player(**p) for p in payload["players"])
                await self.send_report(row["origin"], Victory(**payload))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = "图片生成或群消息发送失败，待自动重试。"
                LOG.warning("PUBG 通知失败（%s），保留待发记录", type(exc).__name__)
                self.store.failed(row)
            else:
                self.store.delivered(row)
