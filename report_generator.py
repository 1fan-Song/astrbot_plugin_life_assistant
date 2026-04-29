import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

from astrbot.api import logger

FONT_CANDIDATES = ["Microsoft YaHei", "SimHei", "STHeiti", "PingFang SC"]

_report_font = None
for _f in FONT_CANDIDATES:
    if any(_f.lower() in f.name.lower() for f in fm.fontManager.ttflist):
        _report_font = _f
        break

if _report_font:
    plt.rcParams["font.sans-serif"] = [_report_font, "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


class ReportGenerator:
    def __init__(self, db, obsidian):
        self.db = db
        self.obsidian = obsidian

    async def generate_report(
        self, session_id: str, period: str = "week",
    ) -> Optional[str]:
        now = datetime.now()
        if period == "week":
            start = now - timedelta(days=7)
            label = "周报"
        elif period == "month":
            start = now.strftime("%Y-%m-01")
            start = datetime.strptime(start, "%Y-%m-%d")
            label = "月报"
        else:
            start = now - timedelta(days=7)
            label = "周报"

        start_str = start.strftime("%Y-%m-%d")
        end_str = now.strftime("%Y-%m-%d")

        report_dir = None
        chart_files = []

        if self.obsidian.enabled:
            report_dir = self.obsidian.vault_path / "报告" / now.strftime("%Y-%m")
            report_dir.mkdir(parents=True, exist_ok=True)

        weight_chart = await self._make_weight_chart(
            session_id, start_str, report_dir,
        )
        if weight_chart:
            chart_files.append(weight_chart)

        expense_chart = await self._make_expense_pie(
            session_id, start_str, report_dir,
        )
        if expense_chart:
            chart_files.append(expense_chart)

        asset_chart = await self._make_asset_line(
            session_id, start_str, report_dir,
        )
        if asset_chart:
            chart_files.append(asset_chart)

        media_summary = await self._get_media_summary(session_id)

        return self._build_markdown(
            label, start_str, end_str,
            chart_files, media_summary, report_dir,
        )

    async def _make_weight_chart(
        self, session_id: str, start: str, report_dir: Optional[Path],
    ) -> Optional[str]:
        records = await self.db.query_health_logs(
            session_id, metric_type="体重", days=60, limit=60,
        )
        records = [r for r in records if r.get("value") is not None]
        if len(records) < 2:
            return None

        records.reverse()
        dates = [r["record_date"][-5:] for r in records]
        values = [r["value"] for r in records]

        fig, ax = plt.subplots(figsize=(8, 3.5))
        ax.plot(dates, values, "o-", color="#4C78A8", markersize=4, linewidth=1.5)
        ax.fill_between(dates, min(values), values, alpha=0.15, color="#4C78A8")
        ax.set_ylabel("体重 (kg)")
        ax.set_title("体重趋势")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()

        return self._save_chart(fig, "weight", report_dir)

    async def _make_expense_pie(
        self, session_id: str, start: str, report_dir: Optional[Path],
    ) -> Optional[str]:
        expenses = await self.db.query_transactions(
            session_id, trans_type="expense", start_date=start, limit=200,
        )
        if not expenses:
            return None

        cat_map: dict[str, float] = {}
        for t in expenses:
            cat = t.get("category") or "其他"
            cat_map[cat] = cat_map.get(cat, 0.0) + t["amount"]

        if not cat_map:
            return None

        sorted_cats = sorted(cat_map.items(), key=lambda x: x[1], reverse=True)
        top = sorted_cats[:6]
        if len(sorted_cats) > 6:
            other_sum = sum(v for _, v in sorted_cats[6:])
            top.append(("其他", other_sum))

        labels = [c[0] for c in top]
        sizes = [c[1] for c in top]
        colors = plt.cm.Set3(range(len(top)))

        fig, ax = plt.subplots(figsize=(6, 4))
        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, autopct="%1.1f%%",
            colors=colors, startangle=140,
            textprops={"fontsize": 9},
        )
        ax.set_title("支出分类占比")
        fig.tight_layout()

        return self._save_chart(fig, "expense", report_dir)

    async def _make_asset_line(
        self, session_id: str, start: str, report_dir: Optional[Path],
    ) -> Optional[str]:
        assets = await self.db.query_transactions(
            session_id, trans_type="asset", limit=30,
        )
        if len(assets) < 2:
            return None

        assets.reverse()
        dates = [a["record_date"][-5:] for a in assets]
        values = [a["amount"] for a in assets]

        fig, ax = plt.subplots(figsize=(8, 3.5))
        ax.plot(dates, values, "o-", color="#54A24B", markersize=5, linewidth=2)
        ax.fill_between(dates, min(values) * 0.98, values, alpha=0.12, color="#54A24B")
        ax.set_ylabel("总资产 (元)")
        ax.set_title("资产变化")
        ax.grid(True, alpha=0.3)

        for i, v in enumerate(values):
            ax.annotate(f"¥{v:,.0f}", (dates[i], v), textcoords="offset points",
                        xytext=(0, 10), ha="center", fontsize=8)

        fig.tight_layout()
        return self._save_chart(fig, "asset", report_dir)

    async def _get_media_summary(self, session_id: str) -> str:
        items = await self.db.query_media_items(session_id)
        if not items:
            return ""
        type_names = {"book": "书", "game": "游戏", "movie": "电影", "music": "音乐", "drama": "剧"}
        status_map: dict[str, list] = {}
        for item in items:
            t = type_names.get(item["media_type"], item["media_type"])
            s = item["status"]
            if s not in status_map:
                status_map[s] = []
            status_map[s].append(f"{t}《{item['title']}》")

        lines = []
        if "done" in status_map:
            lines.append(f"已完成: {', '.join(status_map['done'][:10])}")
        if "doing" in status_map:
            lines.append(f"进行中: {', '.join(status_map['doing'][:5])}")
        if "want" in status_map:
            lines.append(f"待开始: {len(status_map['want'])} 项")
        return "\n".join(lines)

    def _save_chart(self, fig, name: str, report_dir: Optional[Path]) -> Optional[str]:
        ts = datetime.now().strftime("%Y%m%d")
        filename = f"{name}_{ts}.png"
        if report_dir:
            filepath = report_dir / filename
        else:
            import tempfile
            filepath = Path(tempfile.gettempdir()) / f"life_report_{filename}"
        try:
            fig.savefig(str(filepath), dpi=150, bbox_inches="tight")
            plt.close(fig)
            logger.info(f"[LifeAssistant] 报告图表已生成: {filepath}")
            return str(filepath)
        except Exception as e:
            logger.warning(f"[LifeAssistant] 图表生成失败: {e}")
            plt.close(fig)
            return None

    def _build_markdown(
        self, label: str, start: str, end: str,
        charts: list[str], media: str, report_dir: Optional[Path],
    ) -> str:
        now = datetime.now()
        lines = [
            f"# {label} ({start} ~ {end})",
            f"*生成时间: {now.strftime('%Y-%m-%d %H:%M')}*",
            "",
        ]

        chart_labels = {"weight": "体重趋势", "expense": "支出分布", "asset": "资产变化"}

        for chart_path in charts:
            fname = Path(chart_path).name
            cname = fname.split("_")[0]
            ctitle = chart_labels.get(cname, cname)
            if report_dir:
                lines.append(f"## {ctitle}")
                lines.append(f"![{ctitle}]({fname})")
                lines.append("")
            else:
                lines.append(f"## {ctitle}")
                lines.append(f"[图表文件: {chart_path}]")
                lines.append("")

        if media:
            lines.append("## 媒体进度")
            lines.append(media)
            lines.append("")

        md_content = "\n".join(lines)

        if report_dir:
            md_path = report_dir / f"{label}_{now.strftime('%Y%m%d')}.md"
            md_path.write_text(md_content, encoding="utf-8")
            logger.info(f"[LifeAssistant] 报告已写入: {md_path}")

        return md_content
