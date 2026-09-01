import datetime
import inspect
import re

from astrbot.api import logger
from astrbot.api.all import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools

from .core.data import ScheduleData, ScheduleDataManager
from .core.generator import SchedulerGenerator
from .core.schedule import LifeCompanionScheduler
from .core.utils import (
    build_character_state_injection,
    resolve_business_now,
    select_current_activity,
    timeline_to_text,
)


class LifeCompanionPlugin(Star):
    """Keep one coherent daily life state for AstrBot and optional image plugins."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_life_companion")
        self.schedule_data_file = self.data_dir / "schedule_data.json"
        self.data_mgr: ScheduleDataManager | None = None
        self.generator: SchedulerGenerator | None = None
        self.scheduler: LifeCompanionScheduler | None = None

    async def initialize(self):
        self.data_mgr = ScheduleDataManager(self.schedule_data_file)
        self.generator = SchedulerGenerator(self.context, self.config, self.data_mgr)
        self.scheduler = LifeCompanionScheduler(
            context=self.context,
            config=self.config,
            task=self.generator.generate_schedule,
        )
        self.scheduler.start()

    def _save_config(self):
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config()

    def _today(self) -> datetime.datetime:
        return resolve_business_now(self.config.get("schedule_time"))

    @staticmethod
    def _format_reference_umo(umo: str, *, truncate: bool = True) -> str:
        umo = str(umo or "").strip()
        if not umo:
            return "未配置"
        if not truncate or len(umo) <= 20:
            return umo
        return f"{umo[:8]}...{umo[-4:]}（共{len(umo)}字符）"

    async def terminate(self):
        if self.scheduler:
            self.scheduler.stop()

    async def get_life_context(
        self,
        allow_generate: bool = True,
        date: datetime.date | datetime.datetime | None = None,
    ) -> dict:
        """Return daily state for other plugins.

        ``allow_generate=False`` is cache-only, so image plugins never start an
        unexpected LLM request while preparing a prompt.
        """
        if not self.data_mgr or not self.generator:
            return {}

        target = date or self._today()
        data = self.data_mgr.get(target)
        if not data and allow_generate:
            target_datetime = (
                target
                if isinstance(target, datetime.datetime)
                else datetime.datetime.combine(target, datetime.time())
            )
            data = await self.generator.generate_schedule(target_datetime, None)
        if not data or data.status == "failed":
            return {}

        current_activity = select_current_activity(
            data.schedule,
            now=datetime.datetime.now(),
            timeline=data.timeline,
        )
        return {
            "date": data.date,
            "outfit": data.outfit,
            "schedule": data.schedule,
            "timeline": data.timeline,
            "image_prompt": data.image_prompt,
            "current_activity": current_activity,
            "meta": {
                "style": data.outfit_style,
                "source": data.source,
                "generated_at": data.generated_at,
            },
        }

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        business_now = self._today()
        data = self.data_mgr.get(business_now) if self.data_mgr else None
        if not data and self.generator:
            try:
                data = await self.generator.generate_schedule(
                    business_now, event.unified_msg_origin
                )
            except RuntimeError:
                return
        if not data or data.status == "failed":
            return

        inject_text = build_character_state_injection(
            data.outfit,
            data.schedule,
            timeline=data.timeline,
            business_now=business_now,
        )
        req.system_prompt = (req.system_prompt or "") + inject_text
        logger.debug("[LifeCompanion] 已注入生活状态：%s", inject_text)

    @staticmethod
    def _parse_date(value: str | None, today: datetime.datetime) -> datetime.date | None:
        text = str(value or "").strip()
        if not text or text in {"今天", "今日", "today"}:
            return today.date()
        if text in {"昨天", "昨日", "yesterday"}:
            return (today - datetime.timedelta(days=1)).date()
        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            return None

    @staticmethod
    def _format_data(data: ScheduleData) -> str:
        lines = [
            f"📅 {data.date}",
            f"👗 今日穿搭：{data.outfit}",
        ]
        timeline = timeline_to_text(data.timeline)
        if timeline:
            lines.append(f"🕘 时间线：\n{timeline}")
        lines.append(f"📝 日程安排：\n{data.schedule}")
        if data.image_prompt:
            lines.append("📸 已准备今日生活照提示词，可使用：今日生活照")
        return "\n".join(lines)

    async def _get_or_generate_today(
        self, event: AstrMessageEvent, *, force: bool = False, extra: str | None = None
    ) -> ScheduleData | None:
        if not self.data_mgr or not self.generator:
            return None
        today = self._today()
        if not force:
            cached = self.data_mgr.get(today)
            if cached:
                return cached
        return await self.generator.generate_schedule(
            today, event.unified_msg_origin, extra=extra
        )

    @filter.command("查看日程", alias={"life show"})
    async def life_show(self, event: AstrMessageEvent, param: str | None = None):
        today = self._today()
        target = self._parse_date(param, today)
        if not target:
            yield event.plain_result("日期格式错误，请使用 YYYY-MM-DD、今天或昨天")
            return
        data = self.data_mgr.get(target) if self.data_mgr else None
        if not data and target == today.date():
            try:
                yield event.plain_result("今日还没日程，正在生成...")
                data = await self._get_or_generate_today(event)
            except RuntimeError:
                yield event.plain_result("日程正在生成中，请稍后再查看")
                return
        if not data or data.status == "failed":
            yield event.plain_result(f"{target.isoformat()} 暂无可用日程")
            return
        yield event.plain_result(self._format_data(data))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重写日程", alias={"life renew"})
    async def life_renew(self, event: AstrMessageEvent, extra: str | None = None):
        message = (
            f"正在根据补充要求重写今日日程：{extra}"
            if extra
            else "正在重写今日日程..."
        )
        yield event.plain_result(message)
        try:
            data = await self._get_or_generate_today(event, force=True, extra=extra)
        except RuntimeError:
            yield event.plain_result("已有日程生成任务在进行中，请稍后再试")
            return
        if not data or data.status == "failed":
            yield event.plain_result("日程生成失败，请检查 LLM 配置和日志")
            return
        yield event.plain_result(self._format_data(data))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日程时间", alias={"life time"})
    async def life_time(self, event: AstrMessageEvent, param: str | None = None):
        if not param or not re.match(r"^\d{1,2}:\d{1,2}$", param.strip()):
            yield event.plain_result("请提供时间，格式为 HH:MM，例如：日程时间 07:30")
            return
        hour, minute = map(int, param.strip().split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            yield event.plain_result("时间格式错误，小时须为 0-23，分钟须为 0-59")
            return
        try:
            self.scheduler.update_schedule_time(f"{hour:02d}:{minute:02d}")
            yield event.plain_result(
                f"已将每日日程生成时间更新为 {hour:02d}:{minute:02d}。"
            )
        except Exception as exc:
            yield event.plain_result(f"设置失败：{exc}")

    @filter.command("日程历史", alias={"life history"})
    async def life_history(self, event: AstrMessageEvent, param: str | None = None):
        if not self.data_mgr:
            yield event.plain_result("日程数据尚未就绪")
            return
        try:
            limit = max(1, min(int(param or "5"), 10))
        except ValueError:
            limit = 5
        today = self._today()
        current = self.data_mgr.get(today)
        previous = self.data_mgr.history(today, limit)
        if not current and not previous:
            yield event.plain_result("今日还没有日程版本")
            return
        lines = ["今日日程版本（最新在前）："]
        if current:
            lines.append(f"当前：{current.generated_at or '未知时间'} [{current.source}]")
        for index, item in enumerate(previous, 1):
            lines.append(
                f"回滚 {index}：{item.generated_at or '未知时间'} [{item.source}]"
            )
        lines.append("管理员可使用：回滚日程 <编号>")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("回滚日程", alias={"life rollback"})
    async def life_rollback(self, event: AstrMessageEvent, param: str | None = None):
        if not self.data_mgr:
            yield event.plain_result("日程数据尚未就绪")
            return
        try:
            index = max(1, int(param or "1")) - 1
        except ValueError:
            yield event.plain_result("用法：回滚日程 <版本编号>")
            return
        data = self.data_mgr.restore(self._today(), index)
        if not data:
            yield event.plain_result("没有对应的历史版本可回滚")
            return
        yield event.plain_result("已回滚到历史版本：\n" + self._format_data(data))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("导入旧日程", alias={"life import"})
    async def life_import(self, event: AstrMessageEvent, param: str | None = None):
        if not self.data_mgr:
            yield event.plain_result("日程数据尚未就绪")
            return
        legacy_dir = StarTools.get_data_dir("astrbot_plugin_life_scheduler")
        source_path = legacy_dir / "schedule_data.json"
        overwrite = str(param or "").strip().lower() in {
            "覆盖",
            "overwrite",
            "force",
        }
        imported = self.data_mgr.import_file(source_path, overwrite=overwrite)
        if imported:
            suffix = "（已覆盖同日期数据）" if overwrite else ""
            yield event.plain_result(f"已导入 {imported} 天旧日程{suffix}。")
        else:
            yield event.plain_result(
                "没有找到可导入的旧日程，或同日期数据已存在。需要覆盖时使用：导入旧日程 覆盖"
            )

    @filter.command("今日生活照", alias={"life photo"})
    async def life_photo(self, event: AstrMessageEvent):
        """Pass the prepared prompt to the installed gitee_aiimg selfie chain."""
        context = await self.get_life_context(allow_generate=True)
        if not context:
            yield event.plain_result("今日还没有可用日程，无法准备生活照")
            return
        get_registered_star = getattr(self.context, "get_registered_star", None)
        if not callable(get_registered_star):
            yield event.plain_result("未找到 gitee_aiimg 生图插件")
            return
        metadata = get_registered_star("astrbot_plugin_gitee_aiimg")
        image_plugin = getattr(metadata, "star_cls", None)
        handler = getattr(image_plugin, "_do_selfie", None)
        if not callable(handler):
            yield event.plain_result("当前 gitee_aiimg 版本不支持自拍调用")
            return
        prompt = context.get("image_prompt") or (
            f"今日生活照，穿着：{context.get('outfit', '')}；"
            f"活动场景：{context.get('schedule', '')}"
        )
        yield event.plain_result("正在根据今日日程生成生活照...")
        try:
            result = handler(event, prompt)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.error("[LifeCompanion] 今日生活照失败：%s", exc)
            yield event.plain_result("生活照生成失败，请检查自拍参考照和生图插件配置")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("参考会话", alias={"life umo"})
    async def life_reference_umo(self, event: AstrMessageEvent, param: str | None = None):
        action = (param or "set").strip().lower()
        if action in {"", "set"}:
            umo = str(event.unified_msg_origin or "").strip()
            if not umo:
                yield event.plain_result("当前事件没有可保存的会话来源")
                return
            self.config["default_reference_umo"] = umo
            self._save_config()
            yield event.plain_result(
                f"已保存默认参考会话：{self._format_reference_umo(umo)}"
            )
            return
        if action == "show":
            umo = str(self.config.get("default_reference_umo", "") or "").strip()
            yield event.plain_result(
                f"默认参考会话：{self._format_reference_umo(umo, truncate=False)}"
                if umo
                else "默认参考会话未配置"
            )
            return
        if action == "clear":
            self.config["default_reference_umo"] = ""
            self._save_config()
            yield event.plain_result("已清除默认参考会话")
            return
        yield event.plain_result("用法：参考会话 [set|show|clear]")
