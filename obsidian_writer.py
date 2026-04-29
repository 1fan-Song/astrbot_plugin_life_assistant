import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp

from astrbot.api import logger

NOTE_TYPE_FOLDERS = {
    "diary": "日记",
    "reading": "读书笔记",
    "gaming": "游戏笔记",
    "movie": "影视笔记",
    "music": "音乐笔记",
    "drama": "剧评",
}

PROFILE_FILENAMES = {
    "health": "个人健康与运动档案.md",
    "asset": "个人资产配置白皮书.md",
}


class ObsidianWriter:
    def __init__(
        self, vault_path: str, diary_folder: str = "日记",
        notes_folder: str = "笔记", finance_folder: str = "财务",
    ):
        self.vault_path = Path(vault_path) if vault_path and vault_path.strip() else None
        self.diary_folder = diary_folder
        self.notes_folder = notes_folder
        self.finance_folder = finance_folder

    @property
    def enabled(self) -> bool:
        return self.vault_path is not None and self.vault_path.exists()

    def _diary_filepath(self, date_str: str) -> Path:
        date = datetime.strptime(date_str, "%Y-%m-%d")
        year = str(date.year)
        month = f"{date.year}_{date.month:02d}"
        diary_dir = self.vault_path / self.diary_folder / year / month
        diary_dir.mkdir(parents=True, exist_ok=True)
        return diary_dir / f"{date_str}.md"

    def write_diary(
        self, date_str: str, raw_content: str,
        polished_content: str = None,
    ):
        if not self.enabled:
            return
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return
        filepath = self._diary_filepath(date_str)

        content = ""
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8")
        else:
            weekday_names = [
                "周一", "周二", "周三", "周四", "周五", "周六", "周日",
            ]
            weekday = weekday_names[date.weekday()]
            content = (
                f"---\ndate: {date_str}\ntype: 日记\ntags: [日记]\n---\n\n"
                f"# {date_str} {weekday}\n"
            )

        time_str = datetime.now().strftime("%H:%M")
        entry = f"\n## {time_str}\n\n"
        if polished_content:
            entry += polished_content + "\n"
            entry += f"\n> [!note] 原文\n> {raw_content}\n"
        else:
            entry += raw_content + "\n"

        content += entry
        filepath.write_text(content, encoding="utf-8")
        logger.info(f"[LifeAssistant] 日记已写入: {filepath}")

    def append_to_today_diary(self, date_str: str, text: str):
        if not self.enabled:
            return False
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return False
        filepath = self._diary_filepath(date_str)
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8")
        else:
            weekday_names = [
                "周一", "周二", "周三", "周四", "周五", "周六", "周日",
            ]
            weekday = weekday_names[date.weekday()]
            content = (
                f"---\ndate: {date_str}\ntype: 日记\ntags: [日记]\n---\n\n"
                f"# {date_str} {weekday}\n"
            )
        content += f"\n{text}\n"
        filepath.write_text(content, encoding="utf-8")
        return True

    def write_note(
        self, note_type: str, title: str, raw_content: str,
        polished_content: str = None,
    ):
        if not self.enabled:
            return
        type_dir = (
            self.vault_path
            / self.notes_folder
            / NOTE_TYPE_FOLDERS.get(note_type, note_type)
        )
        type_dir.mkdir(parents=True, exist_ok=True)
        safe_title = title.replace("/", "-").replace("\\", "-").replace(":", "-")
        filepath = type_dir / f"{safe_title}.md"

        content = ""
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8")
            content += f"\n\n---\n\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        else:
            type_label = NOTE_TYPE_FOLDERS.get(note_type, note_type)
            content = (
                f"---\ntitle: {title}\ntype: {type_label}\n"
                f"tags: [{note_type}]\n"
                f"created: {datetime.now().strftime('%Y-%m-%d')}\n---\n\n"
                f"# {title}\n\n"
            )

        if polished_content:
            content += polished_content + "\n"
            content += f"\n> [!note] 原文\n> {raw_content}\n"
        else:
            content += raw_content + "\n"

        filepath.write_text(content, encoding="utf-8")
        logger.info(f"[LifeAssistant] 笔记已写入: {filepath}")

    def write_finance_summary(self, year_month: str, summary_text: str):
        if not self.enabled:
            return
        finance_dir = self.vault_path / self.finance_folder
        finance_dir.mkdir(parents=True, exist_ok=True)
        filepath = finance_dir / f"{year_month}.md"

        content = ""
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8")
        else:
            content = (
                f"---\ntype: 财务总结\nperiod: {year_month}\n---\n\n"
                f"# {year_month} 财务总结\n\n"
            )

        content += summary_text + "\n"
        filepath.write_text(content, encoding="utf-8")
        logger.info(f"[LifeAssistant] 财务总结已写入: {filepath}")

    def write_profile(self, profile_type: str, content: str):
        if not self.enabled:
            return
        filename = PROFILE_FILENAMES.get(profile_type)
        if not filename:
            return
        profile_dir = self.vault_path / self.notes_folder
        profile_dir.mkdir(parents=True, exist_ok=True)
        filepath = profile_dir / filename

        type_label = (
            "个人健康与运动档案" if profile_type == "health"
            else "个人资产配置白皮书"
        )
        header = (
            f"---\ntype: 档案\nupdated: "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n---\n\n"
            f"# {type_label}\n\n"
        )
        filepath.write_text(header + content + "\n", encoding="utf-8")
        logger.info(f"[LifeAssistant] 档案已更新: {filepath}")

    async def save_image(
        self, image_url: str, target_folder: str = None,
    ) -> str | None:
        if not self.enabled:
            return None
        attach_dir = self.vault_path / (target_folder or "附件")
        attach_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = uuid.uuid4().hex[:4]

        if os.path.isfile(image_url):
            ext = Path(image_url).suffix or ".png"
            filename = f"img_{ts}_{uid}{ext}"
            filepath = attach_dir / filename
            try:
                shutil.copy2(image_url, filepath)
                logger.info(f"[LifeAssistant] 本地图片已复制: {filepath}")
                return filename
            except Exception as e:
                logger.warning(f"[LifeAssistant] 本地图片复制失败: {e}")
                return None

        ext = ".png"
        if "." in image_url.split("?")[0]:
            ext = "." + image_url.split("?")[0].rsplit(".", 1)[-1][:4]
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
                ext = ".png"
        filename = f"img_{ts}_{uid}{ext}"
        filepath = attach_dir / filename
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            ) as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        logger.warning(f"[LifeAssistant] 图片下载失败: HTTP {resp.status}")
                        return None
                    data = await resp.read()
            filepath.write_bytes(data)
            logger.info(f"[LifeAssistant] 图片已保存: {filepath}")
            return filename
        except Exception as e:
            logger.warning(f"[LifeAssistant] 图片保存失败: {e}")
            return None
