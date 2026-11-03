"""AstrBot entry point. Notifications are rendered locally and sent as images."""
import asyncio
import base64
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .monitor import Monitor, Store
from .pubg_api import PubgClient, PubgError
from .render_card import render_victory, sample_report

HELP = """PUBG 吃鸡战报（Steam）
/pubg 添加 游戏内昵称
/pubg 删除 游戏内昵称
/pubg 列表
/pubg 状态
/pubg 测试

添加/删除/测试需要 AstrBot 管理员或本群群主、管理员权限。
吃鸡后自动发送冠军全队图片战报；首次添加跳过已有战绩。
请在插件配置填写 PUBG API Key，勿发到群里。"""


@register("astrbot_plugin_pubg_chicken", "Local", "Steam PUBG 吃鸡全队图片战报", "1.0.0")
class PubgChickenPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.task = None
        self.client = None
        self.store = None
        self.monitor = None
        self._command_lock = asyncio.Lock()
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_pubg_chicken"

    async def initialize(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / "monitor.sqlite3")
        self.client = PubgClient(self.config.get("api_key", ""), self.config.get("proxy", ""))
        self.monitor = Monitor(
            self.client, self.store, self._send_report,
            include_custom=bool(self.config.get("include_custom_matches", False)),
            max_age_hours=max(1, min(72, int(self.config.get("max_age_hours", 6)))),
            allowed_groups=self.config.get("allowed_group_ids", []),
        )
        self.task = asyncio.create_task(self._run(), name="pubg-chicken-monitor")
        if not self.client.api_key:
            logger.warning("PUBG 吃鸡战报：尚未填写 API Key；配置后请重载插件。")

    async def _run(self):
        # Let adapters finish starting before retrying any saved notifications.
        await asyncio.sleep(10)
        while True:
            if self.config.get("enabled", True) and self.client.api_key:
                try:
                    await self.monitor.poll()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.monitor.last_error = "后台监控异常，将在下一轮重试。"
                    logger.error("PUBG 后台监控异常（%s）", type(exc).__name__)
            await asyncio.sleep(max(30, int(self.config.get("poll_seconds", 60))))

    async def _image_base64(self, report):
        output = self.data_dir / f"card-{uuid.uuid4().hex}.png"

        def render_and_read():
            try:
                render_victory(report, output, self.config.get("font_path", "") or None)
                return base64.b64encode(output.read_bytes()).decode("ascii")
            finally:
                output.unlink(missing_ok=True)

        return await asyncio.to_thread(render_and_read)

    async def _send_report(self, origin, report):
        # Base64 works when NapCat and AstrBot run in separate containers.
        data = await self._image_base64(report)
        sent = await self.context.send_message(origin, MessageChain().base64_image(data))
        if sent is False:
            raise RuntimeError("未找到对应的 AstrBot 平台连接")

    def _can_manage(self, event):
        if event.is_admin():
            return True
        if not self.config.get("allow_group_admin", True):
            return False
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            return raw.get("sender", {}).get("role") in ("owner", "admin")
        return False

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("pubg", alias={"吃鸡战报"})
    async def pubg(self, event: AstrMessageEvent, action: str = "帮助", name: str = ""):
        """PUBG 吃鸡战报：添加/删除/列表/状态/测试。"""
        if not event.get_group_id():
            yield event.plain_result("请在需要接收吃鸡通知的 QQ 群内使用此命令。")
            return
        if self.monitor is None:
            yield event.plain_result("插件还在初始化，请稍后重试。")
            return
        # Subscription identity is the group, even when chat sessions are isolated per member.
        origin = f"{event.get_platform_id()}:GroupMessage:{event.get_group_id()}"
        if not self.monitor.allowed(origin):
            yield event.plain_result("本群未在 PUBG 战报允许群列表中。")
            return
        action = {"add": "添加", "del": "删除", "list": "列表", "status": "状态",
                  "test": "测试", "help": "帮助"}.get(action.lower(), action)
        if action in ("添加", "删除", "测试") and not self._can_manage(event):
            yield event.plain_result("此操作需要 AstrBot 管理员或本群群主、管理员权限。")
            return
        if action == "列表":
            subs = self.store.subscriptions(origin)
            text = "本群 PUBG 监控列表：\n" + "\n".join(f"• {s['name']}" for s in subs)
            yield event.plain_result(text if subs else "本群尚未添加监控账号。")
        elif action == "状态":
            subs = self.store.subscriptions(origin)
            pending = self.store.db.execute(
                "SELECT COUNT(*) FROM outbox WHERE origin=? AND sent_at IS NULL", (origin,),
            ).fetchone()[0]
            state = "已开启" if self.config.get("enabled", True) else "已暂停（插件配置）"
            stamp = "尚未轮询"
            if self.monitor.last_poll:
                stamp = datetime.fromtimestamp(self.monitor.last_poll, timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S")
            text = (f"PUBG 战报：{state}\nAPI Key：{'已配置' if self.client.api_key else '未配置'}\n"
                    f"本群监控：{len(subs)} 个账号\n待发战报：{pending} 张\n上次轮询：{stamp}\n"
                    f"轮询间隔：{max(30, int(self.config.get('poll_seconds', 60)))} 秒（另加请求耗时）")
            if self.monitor.last_error:
                text += "\n最近异常：" + self.monitor.last_error
            yield event.plain_result(text)
        elif action == "测试":
            try:
                data = await self._image_base64(sample_report())
                yield event.chain_result(MessageChain().base64_image(data).chain)
            except Exception as exc:
                logger.warning("PUBG 测试图片生成失败（%s）", type(exc).__name__)
                yield event.plain_result("生成图片失败，请检查插件字体配置和 AstrBot 日志。")
        elif action in ("添加", "删除"):
            if not name:
                yield event.plain_result(f"用法：/pubg {action} PUBG游戏内昵称")
                return
            # Commands can await PUBG without allowing two commands to race a baseline.
            if self._command_lock.locked():
                yield event.plain_result("正在处理账号设置，请稍后再试。")
                return
            async with self._command_lock:
                if action == "删除":
                    removed = self.store.remove(origin, name)
                    yield event.plain_result(f"已移除监控：{name}" if removed else "本群未监控该账号。")
                    return
                if len(self.store.subscriptions(origin)) >= max(1, int(self.config.get("max_players_per_group", 50))):
                    yield event.plain_result("本群监控账号已达配置上限，请先删除部分账号。")
                    return
                try:
                    player = await self.client.find_player(name)
                    added = self.store.subscribe(origin, player)
                    actual_name = player["attributes"]["name"]
                    yield event.plain_result(
                        f"已监控：{actual_name}\n已有战绩已跳过，之后吃鸡会在本群发送全队图片战报。"
                        if added else f"本群已监控 {actual_name}，无需重复添加。"
                    )
                except PubgError as exc:
                    yield event.plain_result(str(exc))
        else:
            yield event.plain_result(HELP)

    async def terminate(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.client:
            await self.client.close()
        if self.store:
            self.store.close()
