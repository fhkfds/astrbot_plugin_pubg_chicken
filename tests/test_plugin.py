"""Offline adapter-contract tests. These simulate AstrBot, not a running QQ bot."""
import base64
from contextlib import contextmanager
import importlib
import io
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from ..monitor import Store, Monitor
from .test_monitor import player


class FakeChain:
    def __init__(self):
        self.chain = []

    def base64_image(self, data):
        self.chain.append({"type": "image", "base64": data})
        return self


def decorator(*args, **kwargs):
    return lambda target: target


class FakeStar:
    def __init__(self, context):
        self.context = context


@contextmanager
def offline_astrbot(data_dir):
    modules = {}
    for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
                 "astrbot.core", "astrbot.core.utils", "astrbot.core.utils.astrbot_path"):
        modules[name] = types.ModuleType(name)
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("test.astrbot")
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = FakeChain
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=decorator, platform_adapter_type=decorator,
        PlatformAdapterType=types.SimpleNamespace(AIOCQHTTP=1),
    )
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = FakeStar
    modules["astrbot.api.star"].register = decorator
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path = lambda: data_dir
    target = "astrbot_plugin_pubg_chicken.main"
    with patch.dict(sys.modules, modules):
        sys.modules.pop(target, None)
        yield importlib.import_module(target).PubgChickenPlugin
    sys.modules.pop(target, None)


class FakeEvent:
    def __init__(self, *, role="member", admin=False, group="12345", sender="100"):
        self.message_obj = types.SimpleNamespace(raw_message={"sender": {"role": role}})
        self.admin, self.group = admin, group
        self.unified_msg_origin = f"napcat:GroupMessage:{sender}_{group}"

    def is_admin(self):
        return self.admin

    def get_group_id(self):
        return self.group

    def get_platform_id(self):
        return "napcat"

    def plain_result(self, text):
        return {"text": text}

    def chain_result(self, chain):
        return {"chain": chain}


async def responses(plugin, event, action, name=""):
    return [item async for item in plugin.pubg(event, action, name)]


class PluginContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.framework = offline_astrbot(self.temp.name)
        cls = self.framework.__enter__()
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.plugin = cls(self.context, {"api_key": "", "enabled": False})
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.framework.__exit__(None, None, None)
        self.temp.cleanup()

    async def test_regular_member_cannot_add_players(self):
        self.plugin.client.find_player = AsyncMock()
        result = await responses(self.plugin, FakeEvent(), "添加", "NightHunter")
        self.assertIn("权限", result[0]["text"])
        self.plugin.client.find_player.assert_not_awaited()

    async def test_group_admin_and_bot_admin_allowed(self):
        self.assertTrue(self.plugin._can_manage(FakeEvent(role="owner")))
        self.assertTrue(self.plugin._can_manage(FakeEvent(role="admin")))
        self.assertTrue(self.plugin._can_manage(FakeEvent(admin=True)))
        self.plugin.config["allow_group_admin"] = False
        self.assertFalse(self.plugin._can_manage(FakeEvent(role="admin")))
        self.assertTrue(self.plugin._can_manage(FakeEvent(admin=True)))

    async def test_subscriptions_are_shared_by_members_in_same_group(self):
        self.plugin.client.find_player = AsyncMock(return_value=player())
        await responses(self.plugin, FakeEvent(role="owner", sender="100"), "添加", "NightHunter")
        result = await responses(self.plugin, FakeEvent(sender="200"), "列表")
        self.assertIn("NightHunter", result[0]["text"])
        self.assertEqual(self.plugin.store.subscriptions()[0]["origin"], "napcat:GroupMessage:12345")

    async def test_private_chat_cannot_register_destination(self):
        result = await responses(self.plugin, FakeEvent(group="", admin=True), "添加", "NightHunter")
        self.assertIn("QQ群", result[0]["text"].replace(" ", ""))
        self.assertEqual(self.plugin.store.subscriptions(), [])

    async def test_preview_is_a_decodable_png_and_leaves_no_temp_files(self):
        result = await responses(self.plugin, FakeEvent(role="owner"), "测试")
        content = base64.b64decode(result[0]["chain"][0]["base64"])
        with Image.open(io.BytesIO(content)) as image:
            self.assertEqual(image.size, (1200, 1317))
            image.verify()
        self.assertEqual(list(self.plugin.data_dir.glob("card-*.png")), [])

    async def test_missing_platform_is_not_acknowledged_as_sent(self):
        self.context.send_message.return_value = False
        self.plugin._image_base64 = AsyncMock(return_value="aGVsbG8=")
        with self.assertRaises(RuntimeError):
            await self.plugin._send_report("missing:GroupMessage:12345", object())

    async def test_termination_cancels_worker_and_closes_session(self):
        await self.plugin.terminate()
        self.assertTrue(self.plugin.task.cancelled())
        self.assertTrue(self.plugin.client.session.closed)
        # Avoid closing the same sqlite connection again in tearDown.
        self.plugin.store = None
