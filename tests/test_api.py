import asyncio
from contextlib import suppress
import time
import unittest

from aiohttp import web

from ..pubg_api import PubgClient, PubgError
from .test_monitor import player


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.status = 200
        self.body = {"data": [player()]}
        app = web.Application()
        app.router.add_get("/{path:.*}", self.respond)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.client = PubgClient("test-key-not-real")
        self.client.BASE = f"http://127.0.0.1:{port}"

    async def respond(self, request):
        self.requests.append((request.path, dict(request.query), dict(request.headers)))
        return web.json_response(self.body, status=self.status, headers={"Retry-After": "90"})

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def test_lookup_uses_game_name_filter_and_bearer_auth(self):
        result = await self.client.find_player("NightHunter")
        self.assertEqual(result["id"], "account.a")
        _, query, headers = self.requests[0]
        self.assertEqual(query, {"filter[playerNames]": "NightHunter"})
        self.assertEqual(headers["Authorization"], "Bearer test-key-not-real")
        self.assertGreater(self.client._next_player_request, time.monotonic() + 6)

    async def test_match_request_has_no_auth_and_uses_unlimited_endpoint(self):
        self.body = {"data": {"id": "m1"}, "included": []}
        await self.client.match("m1")
        self.assertNotIn("Authorization", self.requests[0][2])
        self.assertEqual(self.client._next_player_request, 0)

    async def test_429_applies_retry_after_and_returns_safe_error(self):
        self.status = 429
        with self.assertRaisesRegex(PubgError, "限流"):
            await self.client.players(["account.a"])
        self.assertGreater(self.client._next_player_request, time.monotonic() + 89)

    async def test_429_extends_already_waiting_player_request(self):
        self.client._next_player_request = time.monotonic() + 0.15
        waiter = asyncio.create_task(self.client.players(["account.a"]))
        try:
            await asyncio.sleep(0.01)
            self.status = 429
            with self.assertRaises(PubgError):
                await self.client.match("rate-limited-match")
            await asyncio.sleep(0.18)
            self.assertFalse(waiter.done())
            self.assertEqual(len(self.requests), 1)
        finally:
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter

    async def test_unauthorized_does_not_expose_key(self):
        self.status = 401
        with self.assertRaises(PubgError) as caught:
            await self.client.players(["account.a"])
        self.assertNotIn("test-key-not-real", str(caught.exception))

    async def test_comma_cannot_add_multiple_players_via_one_name(self):
        with self.assertRaises(PubgError):
            await self.client.find_player("Alice,Bob")
        self.assertEqual(self.requests, [])

    async def test_incomplete_player_list_does_not_become_baseline(self):
        self.body = {"data": [{"id": "account.a", "attributes": {"name": "NightHunter"}}]}
        with self.assertRaises(PubgError):
            await self.client.find_player("NightHunter")
