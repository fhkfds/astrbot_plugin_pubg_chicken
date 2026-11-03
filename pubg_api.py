"""Small async client for the official PUBG API; never logs credentials."""
import asyncio
import time
from urllib.parse import quote

import aiohttp


class PubgError(Exception):
    pass


class PubgClient:
    BASE = "https://api.pubg.com/shards/steam"

    def __init__(self, api_key: str, proxy: str = ""):
        self.api_key = api_key.strip()
        self.proxy = proxy.strip() or None
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25), trust_env=True,
        )
        self._limit_lock = asyncio.Lock()
        self._next_player_request = 0.0

    async def close(self):
        await self.session.close()

    async def _request(self, path: str, params=None, limited=True):
        if not self.api_key:
            raise PubgError("请先在插件配置中填写 PUBG API Key。")
        headers = {"Accept": "application/vnd.api+json"}
        if limited:
            headers["Authorization"] = f"Bearer {self.api_key}"
            # Shared by manual lookups and the poller: stay below 10 requests/minute.
            async with self._limit_lock:
                while self._next_player_request > time.monotonic():
                    # A concurrent request can extend the deadline after a 429.
                    await asyncio.sleep(self._next_player_request - time.monotonic())
                self._next_player_request = time.monotonic() + 6.5
        try:
            async with self.session.get(
                self.BASE + path, params=params, headers=headers, proxy=self.proxy,
                allow_redirects=False,
            ) as response:
                if response.status == 429:
                    try:
                        delay = max(60, float(response.headers.get("Retry-After", "60")))
                    except ValueError:
                        delay = 60
                    self._next_player_request = max(
                        self._next_player_request, time.monotonic() + min(delay, 3600),
                    )
                    raise PubgError("PUBG API 请求限流，稍后自动重试。")
                if response.status in (401, 403):
                    raise PubgError("PUBG API Key 无效或没有访问权限，请检查插件配置。")
                if response.status == 404:
                    raise PubgError("PUBG 未找到玩家或对局，请核对游戏内昵称（不是 Steam 昵称）。")
                if response.status != 200:
                    raise PubgError(f"PUBG API 暂时不可用（HTTP {response.status}）。")
                data = await response.json(content_type=None)
                if not isinstance(data, dict) or "data" not in data:
                    raise PubgError("PUBG 返回的数据不完整，稍后重试。")
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError):
            # Do not expose request headers, API responses, or proxy credentials.
            raise PubgError("PUBG 网络请求失败，请检查服务器网络或代理设置。") from None

    async def find_player(self, name: str):
        name = name.strip()
        if not name or len(name) > 64 or "," in name or any(c.isspace() for c in name):
            raise PubgError("请填写一个有效的 PUBG 游戏内昵称。")
        result = await self._request("/players", {"filter[playerNames]": name})
        if not isinstance(result["data"], list):
            raise PubgError("PUBG 玩家列表格式异常。")
        for player in result["data"]:
            if player.get("attributes", {}).get("name", "").casefold() == name.casefold():
                player_matches(player)  # An incomplete response must not become a baseline.
                return player
        raise PubgError("未找到该 PUBG 玩家，请核对大小写和游戏内昵称。")

    async def players(self, account_ids):
        result = await self._request("/players", {"filter[playerIds]": ",".join(account_ids)})
        if not isinstance(result["data"], list):
            raise PubgError("PUBG 玩家列表格式异常。")
        return result["data"]

    async def match(self, match_id):
        return await self._request("/matches/" + quote(match_id, safe=""), limited=False)


def player_matches(player: dict) -> set[str]:
    try:
        if not str(player["id"]).startswith("account."):
            raise ValueError
        matches = player["relationships"]["matches"]["data"]
        if not isinstance(matches, list):
            raise ValueError
        return {str(item["id"]) for item in matches}
    except (KeyError, TypeError, ValueError):
        raise PubgError("PUBG 玩家战绩列表不完整，稍后自动重试。") from None
