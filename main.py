import os
import re
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Plain, Image
from astrbot.core.message.message_event_result import MessageChain

from .database import Database
from .obsidian_writer import ObsidianWriter
from .fund_api import FundAPI
from . import morning_briefing
from . import prompts
from .report_generator import ReportGenerator

MEDIA_TYPE_MAP = {
    "book": "书", "game": "游戏", "movie": "影视", "music": "音乐",
    "drama": "剧",
    "书": "book", "游戏": "game", "影视": "movie", "电影": "movie",
    "音乐": "music", "剧": "drama", "电视剧": "drama", "番": "drama",
}
NOTE_TYPE_MAP = {
    "reading": "读书", "gaming": "游戏", "movie": "影视", "music": "音乐",
    "drama": "追剧",
    "读书": "reading", "游戏": "gaming", "影视": "movie", "电影": "movie",
    "音乐": "music", "剧": "drama", "电视剧": "drama", "追剧": "drama",
}
STATUS_DISPLAY = {
    "want": "想看", "doing": "在看", "done": "已看",
}
NOTE_TYPE_NAMES = {
    "reading": "读书", "gaming": "游戏",
    "movie": "影视", "music": "音乐", "drama": "追剧",
}
MEDIA_ADD_NAMES = {
    "book": "想读", "game": "想玩",
    "movie": "想看", "music": "想听", "drama": "想追",
}
MEDIA_START_NAMES = {
    "book": "在读", "game": "在玩",
    "movie": "在看", "music": "在听", "drama": "在追",
}
MEDIA_TYPE_NAMES = {
    "book": "书", "game": "游戏",
    "movie": "影视", "music": "音乐", "drama": "剧",
}
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class LifeAssistant(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._cached_briefing: str | None = None

        plugin_dir = Path(__file__).parent
        data_dir = plugin_dir / "data"
        data_dir.mkdir(exist_ok=True)

        self.db = Database(str(data_dir / "life_assistant.db"))
        self.obsidian = ObsidianWriter(
            vault_path=self.config.get("obsidian_vault_path", ""),
            diary_folder=self.config.get("obsidian_diary_folder", "日记"),
            notes_folder=self.config.get("obsidian_notes_folder", "笔记"),
            finance_folder=self.config.get("obsidian_finance_folder", "财务"),
        )
        self.auto_polish = self.config.get("auto_polish", True)
        self.auto_categorize = self.config.get("auto_categorize", True)
        self.fund_api = FundAPI()
        self._fund_holdings = self._parse_fund_holdings(
            self.config.get("fund_holdings", "")
        )
        self.report_gen = ReportGenerator(self.db, self.obsidian)

    def _parse_fund_holdings(self, text: str) -> list[dict]:
        holdings = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0] and parts[1]:
                try:
                    holdings.append({
                        "code": parts[0],
                        "shares": float(parts[1]),
                    })
                except ValueError:
                    holdings.append({"code": parts[0], "shares": 0})
            elif len(parts) == 1 and parts[0]:
                holdings.append({"code": parts[0], "shares": 0})
        return holdings

    async def initialize(self):
        logger.info("[LifeAssistant] 插件已加载")
        target = self.config.get("briefing_push_target", "").strip()
        if target:
            briefing_hour = int(self.config.get("briefing_push_hour", 8))
            summary_hour = int(self.config.get("summary_push_hour", 22))
            try:
                cron_mgr = self.context.cron_manager
                await self._cleanup_cron_jobs()
                await cron_mgr.add_basic_job(
                    name="life_briefing",
                    cron_expression=f"0 {briefing_hour} * * *",
                    handler=self._cron_briefing,
                    description="每日晨报推送",
                    timezone="Asia/Shanghai",
                    persistent=True,
                )
                await cron_mgr.add_basic_job(
                    name="life_summary",
                    cron_expression=f"0 {summary_hour} * * *",
                    handler=self._cron_summary,
                    description="每日晚间总结推送",
                    timezone="Asia/Shanghai",
                    persistent=True,
                )
                await cron_mgr.add_basic_job(
                    name="life_weekly_report",
                    cron_expression="30 17 * * 5",
                    handler=self._cron_weekly,
                    description="每周数据周报",
                    timezone="Asia/Shanghai",
                    persistent=True,
                )
                logger.info(
                    f"[LifeAssistant] 定时任务已注册: 晨报 {briefing_hour}:00, "
                    f"总结 {summary_hour}:00, 周报每周五 17:30"
                )
            except Exception as e:
                logger.warning(f"[LifeAssistant] 定时任务注册失败: {e}")
        else:
            logger.info("[LifeAssistant] 未配置 briefing_push_target，跳过定时推送")

        await self._restore_schedule_reminders()

    async def _register_schedule_reminder(self, schedule_id: int):
        item = await self.db.get_schedule(schedule_id)
        if not item or item["status"] != "pending":
            return
        remind_at = item.get("remind_at")
        if not remind_at:
            if item["remind_before"] > 0 and item.get("start_time"):
                try:
                    st = datetime.fromisoformat(item["start_time"])
                    ra = st - timedelta(minutes=item["remind_before"])
                    remind_at = ra.strftime("%Y-%m-%dT%H:%M")
                    await self.db.update_schedule(
                        schedule_id, remind_at=remind_at,
                    )
                except (ValueError, TypeError):
                    return
            else:
                return
        try:
            ra_dt = datetime.fromisoformat(remind_at)
            if ra_dt <= datetime.now():
                return
        except (ValueError, TypeError):
            return
        try:
            cron_mgr = self.context.cron_manager
            if not cron_mgr:
                return
            job_name = f"schedule_remind_{schedule_id}"
            await cron_mgr.delete_job(job_name)
            minute = ra_dt.strftime("%M")
            hour = ra_dt.strftime("%H")
            day = ra_dt.strftime("%d")
            month = ra_dt.strftime("%m")
            cron_expr = f"{minute} {hour} {day} {month} *"
            await cron_mgr.add_basic_job(
                name=job_name,
                cron_expression=cron_expr,
                handler=self._make_remind_handler(schedule_id),
                description=f"日程提醒: {item.get('title', '')}",
                timezone="Asia/Shanghai",
                persistent=False,
            )
        except Exception as e:
            logger.warning(f"[LifeAssistant] 注册日程提醒失败 {schedule_id}: {e}")

    def _make_remind_handler(self, schedule_id: int):
        async def handler():
            item = await self.db.get_schedule(schedule_id)
            if not item or item["status"] != "pending":
                return
            target = self.config.get("briefing_push_target", "").strip()
            if not target:
                return
            priority_names = {"high": "高", "medium": "中", "low": "低"}
            msg = f"别忘了——{item['title']}"
            if item.get("location"):
                msg += f"，在{item['location']}"
            t = item['start_time']
            if "T" in t:
                t = t.replace("T", " ")
            msg += f"\n{t} 开始"
            if item.get("description"):
                msg += f"\n{item['description']}"
            msg += "\n别错过了。"
            await self._push_to_session(target, msg)
            now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await self.db.log_reminder(schedule_id, now_iso)
            if item["schedule_type"] == "one_time":
                await self.db.update_schedule(schedule_id, status="completed")
            elif item.get("recurring_rule"):
                await self._schedule_next_recurring(schedule_id, item)
            try:
                cron_mgr = self.context.cron_manager
                if cron_mgr:
                    await cron_mgr.delete_job(f"schedule_remind_{schedule_id}")
            except Exception:
                pass
        return handler

    async def _schedule_next_recurring(self, schedule_id: int, item: dict):
        try:
            rule = item.get("recurring_rule", "")
            if not rule:
                return
            from croniter import croniter
            base = datetime.now()
            cron = croniter(rule, base)
            next_time = cron.get_next(datetime)
            next_start = next_time.strftime("%Y-%m-%dT%H:%M")
            remind_before = item.get("remind_before", 15)
            next_remind = (next_time - timedelta(minutes=remind_before)).strftime("%Y-%m-%dT%H:%M")
            await self.db.update_schedule(
                schedule_id, start_time=next_start, remind_at=next_remind,
            )
            await self._register_schedule_reminder(schedule_id)
        except ImportError:
            logger.warning("[LifeAssistant] croniter 未安装，无法计算周期日程下次时间")
        except Exception as e:
            logger.warning(f"[LifeAssistant] 计算周期日程下次时间失败: {e}")

    async def _unregister_schedule_reminder(self, schedule_id: int):
        try:
            cron_mgr = self.context.cron_manager
            if cron_mgr:
                await cron_mgr.delete_job(f"schedule_remind_{schedule_id}")
        except Exception:
            pass

    async def _restore_schedule_reminders(self):
        try:
            now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M")
            async with self.db._lock:
                rows = self.db.conn.execute(
                    "SELECT id FROM schedule "
                    "WHERE status = 'pending' AND remind_at IS NOT NULL "
                    "AND remind_at > ?",
                    (now_iso,),
                ).fetchall()
            for row in rows:
                await self._register_schedule_reminder(row[0])
            if rows:
                logger.info(f"[LifeAssistant] 已恢复 {len(rows)} 个日程提醒")
        except Exception as e:
            logger.warning(f"[LifeAssistant] 恢复日程提醒失败: {e}")

    async def _cron_briefing(self):
        target = self.config.get("briefing_push_target", "").strip()
        if not target:
            return
        try:
            text = await self._do_briefing(target)
            self._cached_briefing = text
            logger.info("[LifeAssistant] 晨报已预生成，等待用户触发时发送")
        except Exception as e:
            logger.warning(f"[LifeAssistant] 晨报预生成失败: {e}")

    async def _cron_summary(self):
        target = self.config.get("briefing_push_target", "").strip()
        if not target:
            return
        try:
            result = await self._do_daily_summary(target)
            if isinstance(result, dict):
                text = result["text"]
                success = result.get("success", True)
            else:
                text = result
                success = bool(text)
            if success:
                await self.db.clear_conversation_logs(
                    target, datetime.now().strftime("%Y-%m-%d"),
                )
                logger.info("[LifeAssistant] 对话日志已清空")
            else:
                logger.warning(f"[LifeAssistant] 晚间总结未成功，保留对话日志: {text[:200]}")
            if isinstance(result, dict):
                chain = MessageChain([Plain(text=result["text"])])
                for img_path in result.get("images", []):
                    if os.path.exists(img_path):
                        chain.append(Image.fromFileSystem(img_path))
                await self.context.send_message(target, chain)
            else:
                await self._push_to_session(target, result)
        except Exception as e:
            logger.warning(f"[LifeAssistant] 晚间总结推送失败: {e}")

    async def _cron_weekly(self):
        target = self.config.get("briefing_push_target", "").strip()
        if not target:
            return
        try:
            report = await self.report_gen.generate_report(target, "week")
            if report:
                msg = "上周数据周报已生成，详细图表在 Obsidian 报告目录中。"
                await self._push_to_session(target, msg)
        except Exception as e:
            logger.warning(f"[LifeAssistant] 周报推送失败: {e}")

    async def terminate(self):
        await self._cleanup_cron_jobs()
        await self.fund_api.close()
        self.db.close()
        logger.info("[LifeAssistant] 插件已停止")

    async def _cleanup_cron_jobs(self):
        try:
            cron_mgr = self.context.cron_manager
            if not cron_mgr:
                return
            jobs = await cron_mgr.list_jobs()
            for job in jobs:
                if job.name in ("life_briefing", "life_summary", "life_weekly_report"):
                    await cron_mgr.delete_job(job.job_id)
        except Exception:
            pass

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request):
        if hasattr(request, 'system_prompt') and request.system_prompt is not None:
            request.system_prompt += "\n\n" + prompts.SKILL_PROMPT
        try:
            await self._log_conversation(event, "user")
        except Exception as e:
            logger.debug(f"[LifeAssistant] 记录用户消息失败: {e}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        try:
            await self._log_conversation(event, "assistant", response)
        except Exception as e:
            logger.debug(f"[LifeAssistant] 记录助手消息失败: {e}")

    async def _log_conversation(
        self, event: AstrMessageEvent, role: str, response=None,
    ):
        msg_str = event.get_message_str().strip()
        if not msg_str:
            return
        COMMAND_PREFIXES = (
            "/w ", "/wi ", "/wa ", "/ws", "/d ", "/note ",
            "/polish ", "/m ", "/h ", "/fund", "/briefing",
            "/summary", "/report", "/life",
        )
        if any(msg_str.startswith(p) for p in COMMAND_PREFIXES):
            return
        sid = event.unified_msg_origin
        if role == "user":
            from astrbot.core.message.components import Image as ImageComp
            images = []
            for comp in event.message_obj.message:
                if isinstance(comp, ImageComp):
                    url = comp.url or comp.file or ""
                    if url:
                        filename = await self.obsidian.save_image(url)
                        if filename:
                            images.append(filename)
            content = msg_str
            if images:
                content += " [图片]"
            await self.db.add_conversation_log(
                sid, "user", content, ",".join(images),
            )
        elif role == "assistant" and response:
            text = response.completion_text.strip() if response.completion_text else ""
            if text:
                await self.db.add_conversation_log(sid, "assistant", text)

    async def _get_llm(
        self, session_id: str, prompt: str, system_prompt: str = None,
    ) -> str | None:
        provider_id = await self.context.get_current_chat_provider_id(
            session_id
        )
        if not provider_id:
            return None
        if system_prompt is None:
            try:
                cfg = self.context.get_config(umo=session_id)
                personality_id = cfg.get("provider_settings", {}).get("default_personality", "default")
                persona = self.context.persona_manager.get_persona_v3_by_id(personality_id)
                if persona and persona.get("prompt"):
                    system_prompt = persona["prompt"]
            except Exception:
                pass
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
            return resp.completion_text.strip()
        except Exception as e:
            logger.warning(f"[LifeAssistant] LLM调用失败: {e}")
            return None

    async def _polish(
        self, session_id: str, content: str, prompt_template: str, **kwargs,
    ) -> str | None:
        if not self.auto_polish:
            return None
        p = prompt_template.format(content=content, **kwargs)
        return await self._get_llm(session_id, p)

    async def _categorize(
        self, session_id: str, trans_type: str, amount: float,
        description: str,
    ) -> str:
        if not self.auto_categorize:
            return "其他"
        type_name = "支出" if trans_type == "expense" else "收入"
        prompt = prompts.CATEGORIZE_PROMPT.format(
            type_name=type_name, amount=amount, description=description,
        )
        result = await self._get_llm(session_id, prompt)
        if result and len(result) < 20:
            return result.strip()
        return "其他"

    def _parse_amount(self, text: str) -> float | None:
        if not text:
            return None
        match = re.search(r"[\d.]+", text)
        if match:
            try:
                return float(match.group())
            except ValueError:
                pass
        return None

    # ==================== 快捷命令 ====================

    @filter.command("w")
    async def cmd_expense(self, event: AstrMessageEvent, content: str = ""):
        """快速记支出 /w <金额> [描述]"""
        async for msg in self._do_finance_record(
            event, "expense", content,
        ):
            yield msg

    @filter.command("wi")
    async def cmd_income(self, event: AstrMessageEvent, content: str = ""):
        """快速记收入 /wi <金额> [描述]"""
        async for msg in self._do_finance_record(
            event, "income", content,
        ):
            yield msg

    @filter.command("wa")
    async def cmd_asset(self, event: AstrMessageEvent, content: str = ""):
        """记录资产快照 /wa <总额>"""
        sid = event.unified_msg_origin
        amount = self._parse_amount(content)
        if amount is None:
            yield event.plain_result("请提供金额，例如: /wa 50000")
            return
        await self.db.add_transaction(sid, "asset", amount)
        yield event.plain_result(f"资产快照 ¥{amount:.2f}，记好了。")

    @filter.command("ws")
    async def cmd_ws(
        self, event: AstrMessageEvent, period: str = "month",
    ):
        """财务总结 /ws [today/week/month/year]"""
        async for msg in self._do_finance_query(event, period):
            yield msg

    @filter.command("note")
    async def cmd_note(self, event: AstrMessageEvent, content: str = ""):
        """记笔记 /note <类型> <标题>|<内容>"""
        parts = content.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result(
                "用法: /note <类型> <标题>|<内容>\n"
                "类型: 读书/游戏/影视/音乐"
            )
            return
        note_type = NOTE_TYPE_MAP.get(parts[0], parts[0])
        title_body = parts[1].split("|", 1)
        title = title_body[0].strip()
        body = title_body[1].strip() if len(title_body) > 1 else title
        info = await self._do_note(event, note_type, title, body)
        if info:
            yield event.plain_result(self._format_note_result(info))
        else:
            yield event.plain_result("标题和内容都给我呀")

    @filter.command("polish")
    async def cmd_polish(self, event: AstrMessageEvent, content: str = ""):
        """AI润色 /polish <内容>"""
        if not content:
            yield event.plain_result("请提供需要润色的内容")
            return
        sid = event.unified_msg_origin
        result = await self._polish(sid, content, prompts.POLISH_GENERIC_PROMPT)
        if result:
            yield event.plain_result(
                f"【原文】\n{content}\n\n【润色版】\n{result}"
            )
        else:
            yield event.plain_result("润色没成功，晚点再试试")

    @filter.command_group("m")
    def media_group(self):
        """媒体库管理 /m"""
        pass

    @media_group.command("add")
    async def m_add(self, event: AstrMessageEvent, content: str = ""):
        """添加到媒体库 /m add <类型> <标题>"""
        parts = content.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /m add <类型> <标题>")
            return
        mt = MEDIA_TYPE_MAP.get(parts[0], parts[0])
        title = parts[1]
        async for msg in self._do_media_add(event, mt, title):
            yield msg

    @media_group.command("done")
    async def m_done(self, event: AstrMessageEvent, content: str = ""):
        """标记完成 /m done <类型> <标题> [评分]"""
        parts = content.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /m done <类型> <标题> [评分]")
            return
        mt = MEDIA_TYPE_MAP.get(parts[0], parts[0])
        rating = None
        try:
            rating = float(parts[-1])
            title = " ".join(parts[1:-1])
        except ValueError:
            title = " ".join(parts[1:])
        async for msg in self._do_media_done(event, mt, title, rating):
            yield msg

    @media_group.command("start")
    async def m_start(self, event: AstrMessageEvent, content: str = ""):
        """开始 /m start <类型> <标题>"""
        parts = content.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /m start <类型> <标题>")
            return
        mt = MEDIA_TYPE_MAP.get(parts[0], parts[0])
        title = parts[1]
        async for msg in self._do_media_start(event, mt, title):
            yield msg

    @media_group.command("list")
    async def m_list(self, event: AstrMessageEvent, args: str = ""):
        """查询列表 /m list [类型] [状态]"""
        parts = args.strip().split() if args else []
        media_type = None
        status = None
        for p in parts:
            if p in MEDIA_TYPE_MAP:
                media_type = MEDIA_TYPE_MAP.get(p)
            elif p in ("want", "doing", "done"):
                status = p
            elif p in ("想读", "想看", "想玩", "想听"):
                status = "want"
            elif p in ("在读", "在看", "在玩", "在听"):
                status = "doing"
            elif p in ("已读", "已看", "已玩", "已听"):
                status = "done"
        async for msg in self._do_media_query(event, media_type, status):
            yield msg

    @filter.command_group("h")
    def health_group(self):
        """健康数据 /h"""
        pass

    @health_group.command("log")
    async def h_log(self, event: AstrMessageEvent, content: str = ""):
        """记录健康数据 /h log <类型> <值> [备注]"""
        parts = content.strip().split(maxsplit=2)
        if len(parts) < 2:
            yield event.plain_result(
                "用法: /h log <类型> <值> [备注]\n"
                "示例: /h log 体重 72.5"
            )
            return
        metric_type = parts[0]
        value = None
        try:
            value = float(parts[1])
        except ValueError:
            pass
        note = parts[2] if len(parts) > 2 else None
        async for msg in self._do_health_record(
            event, metric_type, value, note,
        ):
            yield msg

    @health_group.command("query")
    async def h_query(
        self, event: AstrMessageEvent, metric_type: str = "",
    ):
        """查询健康记录 /h query [类型]"""
        async for msg in self._do_health_query(event, metric_type):
            yield msg

    @filter.command("life")
    async def cmd_life(self, event: AstrMessageEvent, content: str = ""):
        """生活助手帮助 /life"""
        help_text = (
            "生活助手命令：\n"
            "/w <金额> [描述] - 快速记支出\n"
            "/wi <金额> [描述] - 快速记收入\n"
            "/wa <总额> - 资产快照\n"
            "/ws [period] - 财务总结\n"
            "/note <类型> <标题>|<内容> - 记笔记\n"
            "/polish <内容> - AI润色\n"
            "/m add/done/start/list - 媒体库\n"
            "/h log/query - 健康数据\n"
            "/fund [代码] - 查看基金涨跌\n"
            "/briefing - 今日晨报\n"
            "/summary - 生成今日日记\n"
            "/report [week/month] - 数据图表报告\n\n"
            "日记会从今天的对话中自动生成。\n"
            "也可以直接用自然语言跟我说，"
            "比如「今天花了35买咖啡」「早上好」"
        )
        yield event.plain_result(help_text)

    @filter.command("fund")
    async def cmd_fund(self, event: AstrMessageEvent, code: str = ""):
        """查看基金 /fund [代码]"""
        sid = event.unified_msg_origin
        if code:
            data = await self.fund_api.get_realtime(code)
            if data:
                rate = data.get("change_rate", 0.0)
                emoji = "🔺" if rate > 0 else "🔻" if rate < 0 else "➖"
                reply = (
                    f"{emoji} {data.get('name', code)}({code})\n"
                    f"估值: {data.get('latest_price', 0):.4f}  "
                    f"涨跌: {rate:+.2f}%"
                )
                if data.get("update_time"):
                    reply += f"\n更新: {data['update_time']}"
            else:
                reply = f"没找到基金 {code}，代码对吗？"
            yield event.plain_result(reply)
            return

        if not self._fund_holdings:
            yield event.plain_result(
                "未配置基金持仓。\n"
                "请在配置中填写 fund_holdings，"
                "格式: 基金代码,份额（每行一个）"
            )
            return

        codes = [h["code"] for h in self._fund_holdings]
        data_list = await self.fund_api.get_batch(codes)
        summary = self.fund_api.format_summary(self._fund_holdings, data_list)
        yield event.plain_result(f"📊 基金持仓\n{summary}")

    @filter.command("briefing")
    async def cmd_briefing(self, event: AstrMessageEvent):
        """今日晨报 /briefing"""
        sid = event.unified_msg_origin
        text = await self._do_briefing(sid)
        yield event.plain_result(text)

    @filter.command("summary")
    async def cmd_summary(self, event: AstrMessageEvent):
        """每日总结 /summary"""
        sid = event.unified_msg_origin
        result = await self._do_daily_summary(sid)
        if isinstance(result, dict):
            chain = MessageChain([Plain(text=result["text"])])
            for img_path in result.get("images", []):
                if os.path.exists(img_path):
                    chain.append(Image.fromFileSystem(img_path))
            yield event.chain_result(chain)
        else:
            yield event.plain_result(result)

    @filter.command("report")
    async def cmd_report(self, event: AstrMessageEvent, period: str = "week"):
        """生成数据报告 /report [week/month]"""
        sid = event.unified_msg_origin
        result = await self.report_gen.generate_report(sid, period)
        if result:
            yield event.plain_result(result[:800])
        else:
            yield event.plain_result("数据还不够呢，攒两天再说吧。")

    # ==================== LLM 工具（自然语言调用） ====================

    @filter.llm_tool("life_record_expense")
    async def tool_expense(
        self, event: AstrMessageEvent, amount: float,
        description: str = "",
    ):
        '''记录一笔支出。当用户提到花钱、买了东西、消费等场景时调用。
        Args:
            amount(number): 支出金额
            description(string): 支出描述，如"买咖啡"、"午饭"
        '''
        sid = event.unified_msg_origin
        category = await self._categorize(
            sid, "expense", amount, description,
        )
        await self.db.add_transaction(
            sid, "expense", amount, category, description,
        )
        today = datetime.now().strftime("%Y-%m-%d")
        if self.obsidian.enabled:
            line = f"- 支出 ¥{amount}"
            if description:
                line += f" ({description}) [{category}]"
            self.obsidian.write_diary(today, line)
        result = f"¥{amount:.2f}"
        if description:
            result += f"（{description}）"
        result += f"记好了，算在{category}里。"
        return result

    @filter.llm_tool("life_record_income")
    async def tool_income(
        self, event: AstrMessageEvent, amount: float,
        description: str = "",
    ):
        '''记录一笔收入。当用户提到收到钱、工资、奖金等场景时调用。
        Args:
            amount(number): 收入金额
            description(string): 收入描述，如"工资"、"红包"
        '''
        sid = event.unified_msg_origin
        category = await self._categorize(
            sid, "income", amount, description,
        )
        await self.db.add_transaction(
            sid, "income", amount, category, description,
        )
        today = datetime.now().strftime("%Y-%m-%d")
        if self.obsidian.enabled:
            line = f"- 收入 ¥{amount}"
            if description:
                line += f" ({description}) [{category}]"
            self.obsidian.write_diary(today, line)
        result = f"¥{amount:.2f} 收入，记上了。"
        if description:
            result = f"¥{amount:.2f}（{description}）记上了。"
        return result

    @filter.llm_tool("life_record_asset")
    async def tool_asset(
        self, event: AstrMessageEvent, total_amount: float,
    ):
        '''记录当前总资产快照。当用户主动告诉你他现在的总资产时调用。
        Args:
            total_amount(number): 当前总资产金额
        '''
        sid = event.unified_msg_origin
        await self.db.add_transaction(sid, "asset", total_amount)
        return f"资产快照 ¥{total_amount:.2f}，记好了。"

    @filter.llm_tool("life_finance_summary")
    async def tool_finance_summary(
        self, event: AstrMessageEvent, period: str = "month",
    ):
        '''查询财务总结。当用户想看花了多少钱、收支统计时调用。
        Args:
            period(string): 时间范围: today/week/month/year
        '''
        sid = event.unified_msg_origin
        summary = await self.db.get_financial_summary(sid, period)

        period_names = {
            "today": "今日", "week": "本周",
            "month": "本月", "year": "今年", "all": "全部",
        }
        lines = [
            f"{period_names.get(period, period)}财务总结：",
            f"收入: ¥{summary['total_income']:.2f}",
            f"支出: ¥{summary['total_expense']:.2f}",
            f"净额: ¥{summary['net']:.2f}",
        ]
        if summary.get("latest_asset"):
            a = summary["latest_asset"]
            lines.append(f"最近资产快照: ¥{a['amount']:.2f} ({a['date']})")
        if summary["category_breakdown"]:
            lines.append("支出分类：")
            for cat in summary["category_breakdown"][:8]:
                lines.append(
                    f"  {cat['category'] or '未分类'}: ¥{cat['total']:.2f}"
                )

        data_text = "\n".join(lines)
        llm_summary = await self._get_llm(
            sid,
            prompts.FINANCE_SUMMARY_PROMPT.format(data=data_text),
        )
        return llm_summary if llm_summary else data_text

    @filter.llm_tool("life_write_note")
    async def tool_note(
        self, event: AstrMessageEvent, note_type: str,
        title: str, content: str,
    ):
        '''记录读书/游戏/影视/音乐/追剧笔记。当用户分享读后感、观后感、游戏体验时调用。
        Args:
            note_type(string): 笔记类型: reading/gaming/movie/music/drama
            title(string): 书名、游戏名、电影名等标题
            content(string): 笔记内容
        '''
        info = await self._do_note(event, note_type, title, content)
        if not info:
            return "标题和内容都给我呀"
        return self._format_note_result(info)

    @filter.llm_tool("life_search_notes")
    async def tool_search_notes(
        self, event: AstrMessageEvent, query: str,
        note_type: str = "",
    ):
        '''搜索已有笔记。当用户问"之前那个游戏的笔记""找找关于XX的笔记"时调用。
        Args:
            query(string): 搜索关键词
            note_type(string): 可选，筛选类型: reading/gaming/movie/music，空字符串表示全部
        '''
        sid = event.unified_msg_origin
        nt = note_type if note_type else None
        results = await self.db.search_notes(sid, query, nt)
        if not results:
            return f"没找到跟"{query}"相关的笔记"
        lines = []
        type_labels = NOTE_TYPE_NAMES
        for r in results:
            label = type_labels.get(r["note_type"], r["note_type"])
            snippet = (r.get("snippet") or "")[:100]
            lines.append(
                f"[{label}] {r['title']}（{r['record_date']}）\n  {snippet}..."
            )
        return "找到这些笔记：\n" + "\n".join(lines)

    @filter.llm_tool("life_polish")
    async def tool_polish(self, event: AstrMessageEvent, content: str):
        '''润色文字。当用户要求帮忙润色、扩写、改写一段文字时调用。
        Args:
            content(string): 需要润色的内容
        '''
        sid = event.unified_msg_origin
        result = await self._polish(
            sid, content, prompts.POLISH_GENERIC_PROMPT,
        )
        return result if result else "润色没成功"

    @filter.llm_tool("life_media_add")
    async def tool_media_add(
        self, event: AstrMessageEvent, media_type: str, title: str,
    ):
        '''添加到想读/想玩/想看列表。当用户想把某本书、游戏、电影加入待办列表时调用。
        Args:
            media_type(string): 类型: book/game/movie/music
            title(string): 标题
        '''
        sid = event.unified_msg_origin
        record_id = await self.db.add_media_item(sid, media_type, title)
        if record_id == -1:
            return f"《{title}》已经在列表里了"
        type_names = MEDIA_ADD_NAMES
        return f"已添加《{title}》到{type_names.get(media_type, media_type)}列表"

    @filter.llm_tool("life_media_done")
    async def tool_media_done(
        self, event: AstrMessageEvent, media_type: str, title: str,
        rating: float = 0,
    ):
        '''标记媒体已完成（已读/已玩/已看）。
        Args:
            media_type(string): 类型: book/game/movie/music
            title(string): 标题
            rating(number): 评分1-10，默认0表示不评分
        '''
        r = rating if rating and rating > 0 else None
        found = False
        async for _ in self._do_media_done(event, media_type, title, r):
            found = True
        if not found:
            return f"没找到《{title}》，要不先加到列表里？"
        reply = f"《{title}》看完了"
        if r:
            reply += f"，评分: {r}/10"
        return reply

    @filter.llm_tool("life_media_query")
    async def tool_media_query(
        self, event: AstrMessageEvent, media_type: str = "",
        status: str = "",
    ):
        '''查询媒体库。当用户问还有哪些书没读完、游戏列表等时调用。
        Args:
            media_type(string): 类型 book/game/movie/music，空字符串表示全部
            status(string): 状态 want/doing/done，空字符串表示全部
        '''
        mt = media_type if media_type else None
        st = status if status else None
        async for msg in self._do_media_query(event, mt, st):
            pass
        items = await self.db.query_media_items(
            event.unified_msg_origin, mt, st,
        )
        if not items:
            return "媒体库中没有匹配的项目"
        type_names = MEDIA_TYPE_NAMES
        status_names = STATUS_DISPLAY
        lines = []
        for item in items:
            s = status_names.get(item["status"], item["status"])
            t = type_names.get(item["media_type"], item["media_type"])
            line = f"[{s}] 《{item['title']}》"
            if item.get("rating"):
                line += f" ⭐{item['rating']}"
            lines.append(line)
        return "\n".join(lines)

    @filter.llm_tool("life_health_record")
    async def tool_health_record(
        self, event: AstrMessageEvent, metric_type: str,
        value: float = 0, note: str = "",
    ):
        '''记录健康数据。当用户提到体重、运动、睡眠等健康信息时调用。
        Args:
            metric_type(string): 指标类型，如体重、跑步、睡眠等
            value(number): 数值，如72.5，默认0
            note(string): 备注说明
        '''
        v = value if value and value > 0 else None
        n = note if note else None
        await self.db.add_health_log(
            event.unified_msg_origin, metric_type, value=v, note=n,
        )
        result = f"{metric_type}"
        if v is not None:
            result += f" {v}"
        if n:
            result += f"（{n}）"
        result += " 记下了。"
        return result

    @filter.llm_tool("life_health_query")
    async def tool_health_query(
        self, event: AstrMessageEvent, metric_type: str = "",
    ):
        '''查询健康记录。当用户想看最近的健康数据时调用。
        Args:
            metric_type(string): 指标类型，空字符串查全部
        '''
        mt = metric_type if metric_type else None
        records = await self.db.query_health_logs(
            event.unified_msg_origin, metric_type=mt, days=30,
        )
        if not records:
            return "还没有健康记录呢"
        lines = [f"最近 {len(records)} 条健康记录："]
        for r in records[:10]:
            val = (
                f"{r['value']}" if r["value"] is not None
                else r.get("value_text") or "-"
            )
            line = f"  {r['record_date']} | {r['metric_type']} | {val}"
            if r.get("note"):
                line += f" | {r['note']}"
            lines.append(line)
        return "\n".join(lines)

    @filter.llm_tool("life_health_analysis")
    async def tool_health_analysis(
        self, event: AstrMessageEvent, metric_type: str = "",
    ):
        '''分析健康数据趋势并给出建议。当用户问身体情况怎么样、健康趋势时调用。
        Args:
            metric_type(string): 指标类型如体重、血压等，空字符串分析全部
        '''
        sid = event.unified_msg_origin
        mt = metric_type if metric_type else None
        records = await self.db.query_health_logs(
            sid, metric_type=mt, days=30,
        )
        if not records:
            return "还没有健康记录呢，先记一些再来分析吧"
        lines = []
        for r in records:
            val = f"{r['value']}" if r["value"] is not None else r.get("value_text") or "-"
            lines.append(f"{r['record_date']} {r['metric_type']}: {val}")
        data_text = "\n".join(lines)
        result = await self._get_llm(
            sid, prompts.HEALTH_TREND_PROMPT.format(data=data_text),
        )
        return result if result else data_text

    @filter.llm_tool("life_fitness_plan")
    async def tool_fitness_plan(
        self, event: AstrMessageEvent, exercise_type: str = "综合",
    ):
        '''生成今日健身训练计划。当用户说今天要健身、推荐训练动作时调用。
        Args:
            exercise_type(string): 训练类型，如力量、有氧、拉伸、核心、综合
        '''
        sid = event.unified_msg_origin
        records = await self.db.query_health_logs(sid, days=7, limit=5)
        health_parts = []
        for r in records:
            val = f"{r['value']}" if r["value"] is not None else r.get("value_text") or "-"
            health_parts.append(f"{r['metric_type']}: {val}")
        health_context = "、".join(health_parts) if health_parts else "无最近数据"
        prompt = prompts.FITNESS_PLAN_PROMPT.format(
            exercise_type=exercise_type, health_context=health_context,
        )
        result = await self._get_llm(sid, prompt)
        return result if result else "训练计划没生成出来，晚点再试试？"

    @filter.llm_tool("life_fund_query")
    async def tool_fund(
        self, event: AstrMessageEvent, fund_code: str = "",
    ):
        '''查询基金涨跌。当用户问基金怎么样、基金涨跌、持仓情况时调用。
        Args:
            fund_code(string): 基金代码，空字符串查全部持仓
        '''
        sid = event.unified_msg_origin
        if fund_code:
            data = await self.fund_api.get_realtime(fund_code)
            if data:
                rate = data.get("change_rate", 0.0)
                emoji = "🔺" if rate > 0 else "🔻" if rate < 0 else "➖"
                reply = (
                    f"{emoji} {data.get('name', fund_code)}({fund_code}) "
                    f"估值 {data.get('latest_price', 0):.4f} "
                    f"涨跌 {rate:+.2f}%"
                )
                return reply
            return f"没找到基金 {fund_code}"

        if not self._fund_holdings:
            return "未配置基金持仓，请在插件配置中添加"
        codes = [h["code"] for h in self._fund_holdings]
        data_list = await self.fund_api.get_batch(codes)
        return "基金持仓：\n" + self.fund_api.format_summary(
            self._fund_holdings, data_list,
        )

    @filter.llm_tool("life_morning_briefing")
    async def tool_briefing(self, event: AstrMessageEvent):
        '''获取今日晨报。必须调用此工具获取晨报，不要自己搜索天气或新闻。当用户说早上好、早安、今日报告、今天怎么样时调用。已预生成，会秒回。'''
        if self._cached_briefing:
            cached = self._cached_briefing
            self._cached_briefing = None
            return cached
        sid = event.unified_msg_origin
        return await self._do_briefing(sid)

    @filter.llm_tool("life_daily_summary")
    async def tool_daily_summary(self, event: AstrMessageEvent):
        '''生成今日总结。当用户说今天过得怎么样、总结一下今天、晚安时调用。'''
        sid = event.unified_msg_origin
        result = await self._do_daily_summary(sid)
        if isinstance(result, dict):
            return result["text"]
        return result

    @filter.llm_tool("life_generate_report")
    async def tool_report(self, event: AstrMessageEvent, period: str = "week"):
        '''生成数据图表报告（体重趋势、支出分布、资产变化）。当用户想看周报、月报、数据图表时调用。
        Args:
            period(string): 报告周期: week(周报) 或 month(月报)
        '''
        sid = event.unified_msg_origin
        result = await self.report_gen.generate_report(sid, period)
        if result:
            return f"报告已生成。\n\n{result[:600]}"
        return "数据还不够呢，攒两天再说吧。"

    @filter.llm_tool("life_save_image")
    async def tool_save_image(
        self, event: AstrMessageEvent, target: str = "diary",
        description: str = "",
    ):
        '''保存用户发送的图片到日记或笔记。当用户发送图片并希望保存时调用。
        Args:
            target(string): 保存目标: diary(日记，默认) 或 note(笔记)
            description(string): 图片描述，可选
        '''
        from astrbot.core.message.components import Image as ImageComp

        images = [
            comp for comp in event.message_obj.message
            if isinstance(comp, ImageComp)
        ]
        if not images:
            return "当前消息中没有图片。"

        today = datetime.now().strftime("%Y-%m-%d")
        saved = []
        for img in images:
            url = img.url or img.file or ""
            if not url or not url.startswith("http"):
                if img.path and os.path.exists(img.path):
                    url = img.path
                else:
                    continue
            filename = await self.obsidian.save_image(url)
            if filename:
                saved.append(filename)

        if not saved:
            return "图片没保存成功，再试一次？"

        embeds = "\n".join(f"![[{f}]]" for f in saved)
        if target == "diary":
            entry = f"\n## {datetime.now().strftime('%H:%M')} 图片\n\n{embeds}\n"
            if description:
                entry += f"\n{description}\n"
            self.obsidian.append_to_today_diary(today, entry)
            return f"已保存{len(saved)}张图片到今日日记。"
        return f"已保存{len(saved)}张图片到附件目录：{', '.join(saved)}"

    @filter.llm_tool("life_update_diary")
    async def tool_update_diary(
        self, event: AstrMessageEvent, content: str,
    ):
        '''更新今天的日记内容。当用户对日记提出修改意见时调用，用新内容替换当天日记的正文部分。
        Args:
            content(string): 更新后的完整日记内容
        '''
        if not self.obsidian.enabled:
            return "Obsidian 未启用。"
        today = datetime.now().strftime("%Y-%m-%d")
        diary_path = self.obsidian._diary_filepath(today)
        if not diary_path or not diary_path.exists():
            return "今天的日记还没写呢。"
        old_content = diary_path.read_text(encoding="utf-8")
        photo_section = ""
        photo_marker = "## 照片"
        if photo_marker in old_content:
            idx = old_content.index(photo_marker)
            photo_section = old_content[idx:]
        new_full = f"---\ndate: {today}\ntype: 日记\ntags: [日记]\n---\n\n# {today}\n\n{content}\n"
        if photo_section:
            new_full += f"\n{photo_section}"
        diary_path.write_text(new_full, encoding="utf-8")
        return "日记已更新。"

    @filter.llm_tool("life_get_profile")
    async def tool_get_profile(
        self, event: AstrMessageEvent, profile_type: str,
    ):
        '''获取用户的健康档案或资产配置白皮书。当需要了解用户基线数据、训练最大重量、投资纪律等信息时调用。
        Args:
            profile_type(string): 档案类型: health(健康档案) 或 asset(资产白皮书)
        '''
        sid = event.unified_msg_origin
        content = await self.db.get_profile(sid, profile_type)
        if content:
            return content
        return f"还没建{'健康' if 'health' in profile_type else '资产'}档案呢"

    @filter.llm_tool("life_update_profile")
    async def tool_update_profile(
        self, event: AstrMessageEvent, profile_type: str, updates: str,
    ):
        '''更新用户健康档案或资产白皮书中的特定字段。当用户提供新的训练数据（如最大重量变化）、投资策略调整等信息时调用。
        Args:
            profile_type(string): 档案类型: health(健康档案) 或 asset(资产白皮书)
            updates(string): 要更新的内容描述，格式为"字段名: 新值"，多项用分号分隔。例如"哑铃卧推: 22.5kg x 8; 坐姿蹬腿: 120kg x 10"
        '''
        sid = event.unified_msg_origin
        current = await self.db.get_profile(sid, profile_type)
        if not current:
            return f"还没建{'健康' if 'health' in profile_type else '资产'}档案呢，先建一个吧"

        type_label = "健康档案" if profile_type == "health" else "资产白皮书"
        prompt = (
            f"以下是用户的当前{type_label}：\n\n{current}\n\n"
            f"用户提供了以下更新信息：{updates}\n\n"
            f"请根据更新信息，输出完整的更新后{type_label}。"
            f"只更新相关字段，其他保持不变。直接输出更新后的完整内容，不要加任何说明。"
        )
        new_content = await self._get_llm(sid, prompt)
        if not new_content:
            return "更新没成功，出了点小状况"

        success = await self.db.update_profile(sid, profile_type, new_content)
        if success:
            if self.obsidian.enabled:
                self.obsidian.write_profile(profile_type, new_content)
            return f"{type_label}已更新：{updates}"
        return "更新没成功，档案找不到了"

    @filter.llm_tool("life_add_schedule")
    async def tool_add_schedule(
        self, event: AstrMessageEvent, title: str, start_time: str,
        end_time: str = "", description: str = "", location: str = "",
        priority: str = "medium", schedule_type: str = "one_time",
        recurring_rule: str = "", recurring_rule_desc: str = "",
        remind_before: int = 15, tags: str = "",
    ):
        '''添加日程。当用户提到日程、待办、计划、安排、会议、约会等时调用。
        Args:
            title(string): 日程标题，如"组会"、"取快递"
            start_time(string): 开始时间，ISO格式，如"2026-04-28T15:00"
            end_time(string): 结束时间，ISO格式，可选
            description(string): 详细描述，可选
            location(string): 地点，如"3楼会议室"，可选
            priority(string): 优先级: high/medium/low，默认medium
            schedule_type(string): 类型: one_time(一次性) 或 recurring(周期性)，默认one_time
            recurring_rule(string): 周期cron表达式，仅recurring类型，如"0 9 * * 1-5"
            recurring_rule_desc(string): 周期规则可读描述，如"工作日每天9点"
            remind_before(int): 提前多少分钟提醒，默认15，0表示不提醒
            tags(string): 标签，逗号分隔，如"科研,组会"
        '''
        sid = event.unified_msg_origin
        schedule_id = await self.db.insert_schedule(
            sid, title, start_time, description, schedule_type,
            end_time or None, location or None, priority, remind_before,
            None, recurring_rule or None, recurring_rule_desc or None,
            tags or None,
        )
        await self._register_schedule_reminder(schedule_id)
        priority_names = {"high": "高", "medium": "中", "low": "低"}
        reply = f"已添加日程「{title}」{start_time}"
        if location:
            reply += f" @ {location}"
        reply += f" [{priority_names.get(priority, priority)}优先级]"
        if remind_before > 0:
            reply += f" 提前{remind_before}分钟提醒"
        if schedule_type == "recurring" and recurring_rule_desc:
            reply += f" ({recurring_rule_desc})"
        return reply

    @filter.llm_tool("life_update_schedule")
    async def tool_update_schedule(
        self, event: AstrMessageEvent, schedule_id: int,
        title: str = "", start_time: str = "", end_time: str = "",
        description: str = "", location: str = "", status: str = "",
        priority: str = "", remind_before: int = -1, tags: str = "",
    ):
        '''修改日程。当用户要求修改、完成、取消日程时调用。
        Args:
            schedule_id(int): 日程ID
            title(string): 新标题，可选
            start_time(string): 新开始时间，ISO格式，可选
            end_time(string): 新结束时间，可选
            description(string): 新描述，可选
            location(string): 新地点，可选
            status(string): 新状态: pending/completed/cancelled，可选
            priority(string): 新优先级: high/medium/low，可选
            remind_before(int): 新提醒分钟数，-1表示不修改
            tags(string): 新标签，逗号分隔，可选
        '''
        updates = {}
        for k, v in [
            ("title", title), ("start_time", start_time),
            ("end_time", end_time), ("description", description),
            ("location", location), ("status", status),
            ("priority", priority), ("tags", tags),
        ]:
            if v:
                updates[k] = v
        if remind_before >= 0:
            updates["remind_before"] = remind_before
        if "end_time" in updates and not updates["end_time"]:
            updates["end_time"] = None
        if not updates:
            return "没有需要修改的字段"
        success = await self.db.update_schedule(schedule_id, **updates)
        if not success:
            return f"没找到这个日程"
        if "status" in updates and updates["status"] in ("completed", "cancelled"):
            await self._unregister_schedule_reminder(schedule_id)
        return f"日程 {schedule_id} 已更新"

    @filter.llm_tool("life_delete_schedule")
    async def tool_delete_schedule(
        self, event: AstrMessageEvent, schedule_id: int,
    ):
        '''删除日程。当用户明确要求删除某个日程时调用。
        Args:
            schedule_id(int): 要删除的日程ID
        '''
        await self._unregister_schedule_reminder(schedule_id)
        success = await self.db.delete_schedule(schedule_id)
        if success:
            return f"日程 {schedule_id} 已删除"
        return f"未找到日程 {schedule_id}"

    @filter.llm_tool("life_query_schedule")
    async def tool_query_schedule(
        self, event: AstrMessageEvent, date: str = "",
        range: str = "today", status: str = "",
        tag: str = "", priority: str = "",
    ):
        '''查询日程。当用户问今天/本周/本月有什么安排、日程、待办时调用。
        Args:
            date(string): 查询日期，格式YYYY-MM-DD，默认今天
            range(string): 查询范围: today/week/month，默认today
            status(string): 筛选状态: pending/completed/cancelled，可选
            tag(string): 按标签筛选，可选
            priority(string): 按优先级筛选: high/medium/low，可选
        '''
        sid = event.unified_msg_origin
        now = datetime.now()
        target_date = date or now.strftime("%Y-%m-%d")
        if range == "week":
            weekday = now.weekday()
            start = (now - timedelta(days=weekday)).strftime("%Y-%m-%d")
            end_date = now + timedelta(days=7 - weekday)
            end = end_date.strftime("%Y-%m-%d")
        elif range == "month":
            start = now.strftime("%Y-%m-01")
            if now.month == 12:
                end = f"{now.year + 1}-01-01"
            else:
                end = f"{now.year}-{now.month + 1:02d}-01"
        else:
            start = target_date
            end = (datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

        items = await self.db.query_schedules(
            sid, start_date=start, end_date=end,
            status=status or None, tag=tag or None,
            priority=priority or None,
        )
        if not items:
            return "这段时间没什么安排"

        priority_names = {"high": "高", "medium": "中", "low": "低"}
        status_icons = {"pending": "⏳", "completed": "✅", "cancelled": "❌"}
        lines = []
        for item in items:
            icon = status_icons.get(item["status"], "·")
            p = priority_names.get(item.get("priority", ""), "")
            time_str = item["start_time"]
            if "T" in time_str:
                time_str = time_str.replace("T", " ")
            line = f"{icon} [{item['id']}] {time_str} {item['title']}"
            if item.get("location"):
                line += f" @ {item['location']}"
            if item.get("priority") and item["priority"] != "medium":
                line += f" [{p}]"
            lines.append(line)
        return "\n".join(lines)

    # ==================== 内部处理方法 ====================

    async def _do_finance_record(
        self, event: AstrMessageEvent, trans_type: str, content: str,
    ):
        sid = event.unified_msg_origin
        amount = self._parse_amount(content)
        if amount is None:
            type_name = "支出" if trans_type == "expense" else "收入"
            yield event.plain_result(
                f"请提供金额，例如: /w 35 买咖啡"
            )
            return

        description = content.strip()
        match = re.search(r"[\d.]+", description)
        if match:
            description = description[match.end():].strip()

        category = await self._categorize(
            sid, trans_type, amount, description,
        )
        await self.db.add_transaction(
            sid, trans_type, amount, category, description,
        )

        type_names = {"expense": "花了", "income": "收到", "asset": "资产"}
        reply = f"¥{amount:.2f}"
        if description:
            reply += f"（{description}）"
        reply += f" {type_names.get(trans_type, '')}，记好了。"
        if trans_type == "expense":
            reply = f"¥{amount:.2f}"
            if description:
                reply += f"（{description}）"
            reply += f"，算在{category}里了。"

        if trans_type != "asset" and self.obsidian.enabled:
            today = datetime.now().strftime("%Y-%m-%d")
            line = f"- {type_names.get(trans_type)} ¥{amount}"
            if description:
                line += f" ({description}) [{category}]"
            self.obsidian.write_diary(today, line)

        yield event.plain_result(reply)

    async def _do_finance_query(
        self, event: AstrMessageEvent, period: str,
    ):
        sid = event.unified_msg_origin
        summary = await self.db.get_financial_summary(sid, period)

        period_names = {
            "today": "今日", "week": "本周",
            "month": "本月", "year": "今年", "all": "全部",
        }
        lines = [
            f"📊 {period_names.get(period, period)}财务总结\n",
            f"收入: ¥{summary['total_income']:.2f}",
            f"支出: ¥{summary['total_expense']:.2f}",
            f"净额: ¥{summary['net']:.2f}",
        ]
        if summary.get("latest_asset"):
            a = summary["latest_asset"]
            lines.append(
                f"\n最近资产快照: ¥{a['amount']:.2f} ({a['date']})"
            )
        if summary["category_breakdown"]:
            lines.append("\n支出分类：")
            for cat in summary["category_breakdown"][:8]:
                lines.append(
                    f"  {cat['category'] or '未分类'}: ¥{cat['total']:.2f}"
                )

        data_text = "\n".join(lines)
        llm_summary = await self._get_llm(
            sid, prompts.FINANCE_SUMMARY_PROMPT.format(data=data_text),
        )
        yield event.plain_result(llm_summary if llm_summary else data_text)

    async def _do_note(
        self, event: AstrMessageEvent, note_type: str,
        title: str, content: str,
    ) -> dict | None:
        sid = event.unified_msg_origin
        if not content or not title:
            return None

        type_label = NOTE_TYPE_MAP.get(note_type, note_type)
        polished = await self._polish(
            sid, content, prompts.POLISH_NOTE_PROMPT,
            note_type=type_label, title=title,
        )
        today = datetime.now().strftime("%Y-%m-%d")
        await self.db.add_note(
            sid, note_type, title, content, polished, today,
        )

        if self.obsidian.enabled:
            self.obsidian.write_note(note_type, title, content, polished)

        return {
            "note_type": note_type,
            "title": title,
            "raw": content,
            "polished": polished,
        }

    def _format_note_result(self, info: dict) -> str:
        label = NOTE_TYPE_NAMES.get(info["note_type"], info["note_type"])
        result = f"--- {label}笔记《{info['title']}》 ---\n\n"
        result += f"【原文】\n{info['raw']}\n\n"
        if info["polished"]:
            result += f"【润色版】（已写入 Obsidian）\n{info['polished']}\n\n"
        result += "如果想修改，跟我说就行。"
        return result

    async def _do_media_add(
        self, event: AstrMessageEvent, media_type: str, title: str,
    ):
        sid = event.unified_msg_origin
        if not title:
            yield event.plain_result("请提供标题")
            return

        record_id = await self.db.add_media_item(sid, media_type, title)
        if record_id == -1:
            yield event.plain_result(f"《{title}》已经在列表里了")
            return

        type_names = MEDIA_ADD_NAMES
        yield event.plain_result(
            f"已添加《{title}》到{type_names.get(media_type, media_type)}列表"
        )

    async def _do_media_done(
        self, event: AstrMessageEvent, media_type: str,
        title: str, rating: float = None,
    ):
        sid = event.unified_msg_origin
        if not title:
            yield event.plain_result("请提供标题")
            return

        success = await self.db.update_media_item(
            sid, media_type, title, status="done", rating=rating,
        )
        if success:
            reply = f"《{title}》看完了，不错嘛"
            if rating:
                reply += f"，评分: {rating}/10"
            yield event.plain_result(reply)
        else:
            yield event.plain_result(
                f"没找到《{title}》，要不先加到列表里？"
            )

    async def _do_media_start(
        self, event: AstrMessageEvent, media_type: str, title: str,
    ):
        sid = event.unified_msg_origin
        if not title:
            yield event.plain_result("请提供标题")
            return

        success = await self.db.update_media_item(
            sid, media_type, title, status="doing",
        )
        if success:
            type_names = MEDIA_START_NAMES
            yield event.plain_result(
                f"已标记《{title}》为{type_names.get(media_type, '进行中')}"
            )
        else:
            yield event.plain_result(
                f"没找到《{title}》，要不先加到列表里？"
            )

    async def _do_media_query(
        self, event: AstrMessageEvent, media_type: str = None,
        status: str = None,
    ):
        sid = event.unified_msg_origin
        items = await self.db.query_media_items(sid, media_type, status)
        if not items:
            yield event.plain_result("媒体库中没有匹配的项目")
            return

        type_names = MEDIA_TYPE_NAMES
        status_names = STATUS_DISPLAY

        lines = []
        filter_desc = ""
        if media_type:
            filter_desc += type_names.get(media_type, media_type)
        if status:
            filter_desc += status_names.get(status, status)
        header = f"📋 {filter_desc}列表：\n" if filter_desc else "📋 媒体库：\n"
        lines.append(header)

        for item in items:
            s = status_names.get(item["status"], item["status"])
            line = f"  [{s}] 《{item['title']}》"
            if item.get("rating"):
                line += f" ⭐{item['rating']}"
            lines.append(line)

        yield event.plain_result("\n".join(lines))

    async def _do_health_record(
        self, event: AstrMessageEvent, metric_type: str,
        value: float = None, note: str = None,
    ):
        sid = event.unified_msg_origin
        if not metric_type:
            yield event.plain_result(
                "请提供记录类型，例如: /h log 体重 72.5"
            )
            return

        await self.db.add_health_log(
            sid, metric_type, value=value, note=note,
        )
        reply = f"{metric_type}"
        if value is not None:
            reply += f" {value}"
        if note:
            reply += f"（{note}）"
        reply += " 记下了。"
        yield event.plain_result(reply)

    async def _do_health_query(
        self, event: AstrMessageEvent, metric_type: str = None,
    ):
        sid = event.unified_msg_origin
        records = await self.db.query_health_logs(
            sid, metric_type=metric_type, days=30,
        )
        if not records:
            yield event.plain_result("还没有健康记录呢")
            return

        lines = [f"最近 {len(records)} 条健康记录：\n"]
        for r in records[:10]:
            val = (
                f"{r['value']}" if r["value"] is not None
                else r.get("value_text") or "-"
            )
            line = f"  {r['record_date']} | {r['metric_type']} | {val}"
            if r.get("note"):
                line += f" | {r['note']}"
            lines.append(line)

        yield event.plain_result("\n".join(lines))

    async def _do_briefing(self, session_id: str) -> str:
        async def llm_call(prompt: str) -> str | None:
            return await self._get_llm(session_id, prompt)

        news_data = await morning_briefing.fetch_ai_news()
        if news_data:
            news_summary = await morning_briefing.summarize_news(
                llm_call, news_data["content"],
            )
        else:
            news_summary = "暂无法获取今日AI资讯"

        if self._fund_holdings:
            codes = [h["code"] for h in self._fund_holdings]
            data_list = await self.fund_api.get_batch(codes)
            today = datetime.now()
            today_str = today.strftime("%Y-%m-%d")
            valid = [d for d in data_list if not d.get("error")]
            stale = False
            if valid:
                max_update = max(d.get("update_time", "") for d in valid)
                update_dt = None
                try:
                    update_dt = datetime.strptime(max_update, "%Y-%m-%d")
                except (ValueError, TypeError):
                    pass
                if update_dt:
                    gap = (today - update_dt).days
                    wd = today.weekday()
                    if wd == 0:
                        stale = gap > 3
                    elif wd >= 5:
                        stale = gap > (wd - 4)
                    else:
                        stale = gap > 1
            else:
                stale = True
            if stale:
                data_list = []
                fund_summary = "今日无最新净值（休市或净值未更新）"
            else:
                fund_summary = self.fund_api.format_summary(
                    self._fund_holdings, data_list,
                )
        else:
            data_list = []
            fund_summary = "未配置基金持仓"

        weather = await morning_briefing.fetch_weather()
        world_news = await morning_briefing.fetch_world_news()
        city_name = self.config.get("weather_city_name", "Shenzhen").strip()

        briefing = await morning_briefing.generate_briefing(
            llm_call, fund_summary, news_summary, weather, world_news,
            fund_data_list=data_list, city=city_name,
        )

        today = datetime.now().strftime("%Y-%m-%d")
        weekday = WEEKDAY_NAMES[datetime.now().weekday()]
        header = f"{today} {weekday} 晨报\n{'=' * 24}\n\n"

        schedule_section = ""
        next_day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        today_schedules = await self.db.query_schedules(
            session_id, start_date=today, end_date=next_day, status="pending",
        )
        if today_schedules:
            schedule_section = "今日待办：\n"
            for s in today_schedules:
                t = s["start_time"]
                if "T" in t:
                    t = t.split("T")[1][:5]
                line = f"  ⏳ {t} {s['title']}"
                if s.get("location"):
                    line += f" @ {s['location']}"
                schedule_section += line + "\n"
            schedule_section += "\n"

        return header + schedule_section + briefing

    async def _do_daily_summary(self, session_id: str) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        conv_logs = await self.db.query_conversation_logs(session_id, today)
        y_conv_logs = await self.db.query_conversation_logs(session_id, yesterday)

        target_date = today
        if not conv_logs and y_conv_logs:
            conv_logs = y_conv_logs
            target_date = yesterday

        expense_data, income_data, health_data = await asyncio.gather(
            self.db.query_transactions(
                session_id, trans_type="expense",
                start_date=target_date, end_date=target_date,
            ),
            self.db.query_transactions(
                session_id, trans_type="income",
                start_date=target_date, end_date=target_date,
            ),
            self.db.query_health_logs(session_id, days=1, limit=10),
        )

        if not conv_logs and not expense_data and not income_data and not health_data:
            return {"text": "今天还没有任何记录呢。好好休息吧。", "images": [], "success": False}

        conv_text = ""
        all_images = []
        for log in conv_logs:
            role_label = "用户" if log["role"] == "user" else "青衿"
            time_prefix = log.get("created_at", "")[11:16]
            conv_text += f"[{time_prefix}] {role_label}: {log['content']}\n"
            if log.get("images"):
                for img in log["images"].split(","):
                    if img.strip():
                        all_images.append(img.strip())

        image_paths = []
        if self.obsidian.enabled and all_images:
            attach_dir = self.obsidian.vault_path / "附件"
            for img_name in all_images:
                p = attach_dir / img_name
                if p.exists():
                    image_paths.append(str(p))

        finance_text = ""
        if expense_data or income_data:
            total_exp = sum(t["amount"] for t in expense_data)
            total_inc = sum(t["amount"] for t in income_data)
            finance_text = f"收入: ¥{total_inc:.2f}，支出: ¥{total_exp:.2f}\n"
            for t in expense_data[:10]:
                desc = f" ¥{t['amount']:.0f}"
                if t.get("description"):
                    desc += f" ({t['description']})"
                finance_text += f"  - {t.get('category', '未分类')}{desc}\n"

        health_text = ""
        if health_data:
            for h in health_data:
                val = f"{h['value']}" if h["value"] is not None else h.get("value_text") or "-"
                health_text += f"  {h['metric_type']}: {val}"
                if h.get("note"):
                    health_text += f"（{h['note']}）"
                health_text += "\n"

        next_day = (datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        schedule_items = await self.db.query_schedules(
            session_id, start_date=target_date, end_date=next_day,
        )
        schedule_text = ""
        if schedule_items:
            status_icons = {"pending": "⏳", "completed": "✅", "cancelled": "❌"}
            for s in schedule_items:
                icon = status_icons.get(s["status"], "·")
                t = s["start_time"]
                if "T" in t:
                    t = t.split("T")[1][:5]
                schedule_text += f"  {icon} {t} {s['title']}"
                if s.get("location"):
                    schedule_text += f" @ {s['location']}"
                schedule_text += "\n"

        raw_prompt = prompts.DIARY_RAW_PROMPT.format(
            date=target_date,
            conversations=conv_text or "（无对话记录）",
            finance=finance_text or "（无收支记录）",
            health=health_text or "（无健康记录）",
            image_count=len(all_images),
        )
        if schedule_text:
            raw_prompt += f"\n\n今日日程：\n{schedule_text}"

        gaming_notes = await self.db.query_notes(
            session_id, note_type="gaming", days=90, limit=30,
        )
        if gaming_notes:
            note_list = ", ".join(n["title"] for n in gaming_notes if n.get("title"))
            raw_prompt += f"\n\n已有游戏笔记：{note_list}"
        raw_diary = await self._get_llm(session_id, raw_prompt)
        if not raw_diary:
            return {"text": "日记没写出来，晚点再试试吧。", "images": [], "success": False}

        polished_diary = await self._get_llm(
            session_id,
            prompts.DIARY_POLISHED_PROMPT.format(content=raw_diary),
        ) or raw_diary

        obsidian_diary = polished_diary
        if all_images:
            for i, img_name in enumerate(all_images, 1):
                obsidian_diary = obsidian_diary.replace(
                    f"[照片{i}]", f"\n![[{img_name}]]\n", 1,
                )
            obsidian_diary = re.sub(r'\[照片\d+\]', '', obsidian_diary)

        if self.obsidian.enabled:
            self.obsidian.write_diary(target_date, obsidian_diary)

        result = (
            f"--- {target_date} 日记 ---\n\n"
            f"【原话提炼版】\n{raw_diary}\n\n"
            f"【润色版】（已写入 Obsidian）\n{polished_diary}\n\n"
        )
        if all_images:
            result += "照片编号对应日记中 [照片X] 标记的位置。\n"
        result += "如果想修改，跟我说就行。"
        return {"text": result, "images": image_paths, "success": True}

    async def _push_to_session(self, session_id: str, text: str):
        try:
            chain = MessageChain([Plain(text=text)])
            await self.context.send_message(session_id, chain)
            logger.info(f"[LifeAssistant] 已推送消息到 {session_id}")
        except Exception as e:
            logger.warning(f"[LifeAssistant] 推送失败: {e}")


