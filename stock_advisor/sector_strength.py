"""
板块强度排名 — Sector Strength Ranking

核心理念：
  - 每天扫一遍 A 股所有行业板块，按涨跌幅排名
  - 找出最强板块（资金在往哪里流）和最弱板块（资金在从哪里撤）
  - 持仓标的属于哪个板块，板块强则加分，板块弱则减分
  - 接入盘前简报，帮助判断「今天该买什么方向」

数据来源：东方财富行业板块 API
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

import requests

logger = logging.getLogger(__name__)


@dataclass
class SectorInfo:
    """板块信息"""
    code: str           # 板块代码 (BKxxxx)
    name: str           # 板块名称
    change_pct: Decimal # 涨跌幅%
    leading_stock: str  # 领涨股
    up_count: int       # 上涨家数
    down_count: int     # 下跌家数


# Known sector mappings for our holdings
# Symbol pattern → sector name
STOCK_SECTOR_MAP = {
    "sh601698": "航天航空",    # 中国卫通
    "sz000063": "通信设备",    # 中兴通讯
    "sz002439": "网络安全",    # 启明星辰
}


def fetch_sector_boards(top_n: int = 50) -> list[SectorInfo]:
    """Fetch industry sector board data from East Money.
    
    API: push2.eastmoney.com/api/qt/clist/get
    fs=m:90+t:2 → A股行业板块
    """
    try:
        resp = requests.get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": "1",
                "pz": str(top_n),
                "po": "1",             # sort by change desc
                "np": "1",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",           # sort by change percent
                "fs": "m:90+t:2",      # industry sectors
                "fields": "f2,f3,f4,f12,f14,f104,f105",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        
        sectors = []
        for item in data.get("diff") or []:
            try:
                sectors.append(SectorInfo(
                    code=item.get("f12", ""),
                    name=item.get("f14", ""),
                    change_pct=Decimal(str(item.get("f3", 0))),
                    leading_stock=item.get("f104", "") or "",
                    up_count=int(item.get("f104", 0) or 0),
                    down_count=int(item.get("f105", 0) or 0),
                ))
            except Exception:
                continue
        
        return sectors
    
    except Exception as exc:
        logger.warning("Failed to fetch sector boards: %s", exc)
        return []


def get_sector_strength(sectors: list[SectorInfo], sector_name: str) -> Decimal | None:
    """Get a specific sector's change percent."""
    for s in sectors:
        if s.name == sector_name:
            return s.change_pct
    return None


def get_top_sectors(sectors: list[SectorInfo], n: int = 5) -> list[SectorInfo]:
    """Get top N performing sectors (already sorted desc by API)."""
    return sectors[:n]


def get_bottom_sectors(sectors: list[SectorInfo], n: int = 5) -> list[SectorInfo]:
    """Get bottom N performing sectors."""
    return sectors[-n:] if len(sectors) >= n else sectors


def get_holding_sector_info(sectors: list[SectorInfo], symbol: str) -> SectorInfo | None:
    """Get sector info for a holding based on symbol mapping."""
    sector_name = STOCK_SECTOR_MAP.get(symbol)
    if not sector_name:
        return None
    for s in sectors:
        if s.name == sector_name:
            return s
    return None


def format_sector_report(sectors: list[SectorInfo], holdings: list[dict] | None = None) -> str:
    """Format sector ranking as markdown for briefing/review.
    
    Args:
        sectors: List of SectorInfo from fetch_sector_boards()
        holdings: Optional list of {"symbol": "sh601698", "name": "中国卫通"} for annotation
    """
    if not sectors:
        return "📊 板块数据获取失败"
    
    top5 = get_top_sectors(sectors, 5)
    bottom5 = get_bottom_sectors(sectors, 5)
    
    lines = ["📊 **行业板块强度**"]
    lines.append("")
    
    # Top sectors
    lines.append("**🔥 领涨板块 Top 5**")
    for i, s in enumerate(top5, 1):
        emoji = "🔴" if s.change_pct >= 2 else "🟠" if s.change_pct >= 1 else "🟡"
        lines.append(f"  {emoji} {i}. **{s.name}** {s.change_pct:+.2f}%")
    
    lines.append("")
    
    # Bottom sectors
    lines.append("**❄️ 领跌板块 Bottom 5**")
    for i, s in enumerate(bottom5, 1):
        emoji = "🟢" if s.change_pct >= -1 else "🔵"
        lines.append(f"  {emoji} {i}. **{s.name}** {s.change_pct:+.2f}%")
    
    # Holdings annotation
    if holdings:
        lines.append("")
        lines.append("**📌 持仓板块表现**")
        for h in holdings:
            symbol = h.get("symbol", "")
            name = h.get("name", "")
            sector_name = STOCK_SECTOR_MAP.get(symbol)
            if sector_name:
                strength = get_sector_strength(sectors, sector_name)
                if strength is not None:
                    emoji = "🔥" if strength >= 2 else "✅" if strength > 0 else "⚠️" if strength > -2 else "❌"
                    lines.append(f"  {emoji} {name} → {sector_name} {strength:+.2f}%")
                else:
                    lines.append(f"  ❓ {name} → {sector_name}（未找到）")
            else:
                lines.append(f"  ❓ {name} → 板块未映射")
    
    return "\n".join(lines)


def compute_sector_score_boost(sectors: list[SectorInfo], symbol: str) -> int:
    """Compute a score adjustment based on sector strength.
    
    Returns score adjustment (-5 to +5) based on sector position.
    """
    sector_name = STOCK_SECTOR_MAP.get(symbol)
    if not sector_name or not sectors:
        return 0
    
    # Find sector and its rank
    for rank, s in enumerate(sectors, 1):
        if s.name == sector_name:
            total = len(sectors)
            pct_rank = rank / total
            
            if pct_rank <= 0.1:       # Top 10%
                return 5
            elif pct_rank <= 0.2:     # Top 20%
                return 3
            elif pct_rank <= 0.4:     # Top 40%
                return 1
            elif pct_rank >= 0.9:     # Bottom 10%
                return -5
            elif pct_rank >= 0.8:     # Bottom 20%
                return -3
            else:
                return 0
    
    return 0
