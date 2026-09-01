import datetime
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Union

# =========================
# 类型定义
# =========================

ScheduleStatus = Literal["ok", "failed", "manual"]

DateLike = Union[  # noqa: UP007
    datetime.datetime,
    datetime.date,
    str,  # yyyy-mm-dd
    int,  # timestamp
    float,  # timestamp
]


# =========================
# 工具函数（时间归一化）
# =========================


def to_date_str(value: DateLike) -> str:
    """统一将时间输入转为 yyyy-mm-dd 字符串"""
    if isinstance(value, str):
        parsed = datetime.date.fromisoformat(value.strip())
        return parsed.isoformat()
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, int | float):
        return datetime.datetime.fromtimestamp(value).date().isoformat()
    raise TypeError(f"Unsupported date type: {type(value)}")


# =========================
# 数据结构
# =========================


@dataclass(slots=True)
class ScheduleData:
    """单日生活状态。

    ``timeline`` and ``image_prompt`` were added by Life Companion.  Missing
    fields are intentionally accepted so old exported schedule data remains
    readable.
    """

    date: str  # yyyy-mm-dd
    outfit_style: str = ""
    outfit: str = ""
    schedule: str = ""
    timeline: list[dict[str, str]] = field(default_factory=list)
    image_prompt: str = ""
    generated_at: str = ""
    source: str = "llm"
    status: ScheduleStatus = "ok"

    @classmethod
    def from_dict(cls, data: dict, *, date: str | None = None) -> "ScheduleData":
        """允许未来字段扩展"""
        return cls(
            date=str(data.get("date") or date or ""),
            outfit_style=data.get("outfit_style", ""),
            outfit=data.get("outfit", ""),
            schedule=data.get("schedule", ""),
            timeline=normalize_timeline(data.get("timeline")),
            image_prompt=str(data.get("image_prompt", "") or "").strip(),
            generated_at=str(data.get("generated_at", "") or "").strip(),
            source=str(data.get("source", "llm") or "llm").strip(),
            status=data.get("status", "ok"),
        )


def normalize_timeline(value: Any) -> list[dict[str, str]]:
    """Keep model-provided timeline data small and predictable."""
    if not isinstance(value, list):
        return []

    result: list[dict[str, str]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        time_value = str(item.get("time", "") or "").strip()
        activity = str(
            item.get("activity") or item.get("title") or item.get("text") or ""
        ).strip()
        time_match = re.match(
            r"^\s*([01]?\d|2[0-3])[:：]([0-5]?\d)\s*$", time_value
        )
        if not time_match or not activity:
            continue
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        entry = {"time": f"{hour:02d}:{minute:02d}", "activity": activity[:160]}
        for key in ("location", "mood", "image_hint"):
            value_text = str(item.get(key, "") or "").strip()
            if value_text:
                entry[key] = value_text[:120]
        result.append(entry)
    return sorted(
        result,
        key=lambda item: int(item["time"][:2]) * 60 + int(item["time"][3:]),
    )


# =========================
# 数据管理器（纯存取）
# =========================


class ScheduleDataManager:
    """
    纯数据层：
    - 内存存取
    - JSON 持久化
    """

    def __init__(self, json_path: Path):
        self._path = json_path
        self._history_path = json_path.with_name("schedule_history.json")
        self._data: dict[str, ScheduleData] = {}
        self._history: dict[str, list[ScheduleData]] = {}

        self.load()
        self._load_history()

    # ---------- 基础 CRUD ----------

    def has(self, date: DateLike) -> bool:
        return to_date_str(date) in self._data

    def get(self, date: DateLike) -> ScheduleData | None:
        return self._data.get(to_date_str(date))

    def set(self, data: ScheduleData, *, record_history: bool = True) -> None:
        previous = self._data.get(data.date)
        if previous and previous != data and record_history:
            snapshots = self._history.setdefault(data.date, [])
            snapshots.append(ScheduleData.from_dict(asdict(previous)))
            self._history[data.date] = snapshots[-10:]
        self._data[data.date] = data
        self.save()
        if previous and previous != data and record_history:
            self._save_history()

    def remove(self, date: DateLike) -> None:
        if self._data.pop(to_date_str(date), None):
            self.save()

    def all(self) -> dict[str, ScheduleData]:
        """返回副本，防止外部污染"""
        return dict(self._data)

    def history(self, date: DateLike, limit: int = 10) -> list[ScheduleData]:
        """Return previous versions, newest first."""
        if limit <= 0:
            return []
        return list(reversed(self._history.get(to_date_str(date), [])[-limit:]))

    def restore(self, date: DateLike, index: int = 0) -> ScheduleData | None:
        """Restore a previous version and keep the current version recoverable."""
        snapshots = self.history(date)
        if index < 0 or index >= len(snapshots):
            return None
        previous = snapshots[index]
        restored = ScheduleData.from_dict(
            {
                **asdict(previous),
                "source": "rollback",
                "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.set(restored)
        return restored

    def import_file(self, source_path: Path, *, overwrite: bool = False) -> int:
        """Import compatible schedule JSON, returning the number of imported days."""
        try:
            raw = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(raw, dict):
            return 0

        imported = 0
        for date_str, item in raw.items():
            if not isinstance(item, dict) or (date_str in self._data and not overwrite):
                continue
            try:
                data = ScheduleData.from_dict(item, date=str(date_str))
            except (KeyError, TypeError, ValueError):
                continue
            if not data.date:
                continue
            self.set(
                ScheduleData.from_dict({**asdict(data), "source": "import"}),
                record_history=overwrite,
            )
            imported += 1
        return imported

    # ---------- JSON 持久化 ----------

    def load(self) -> None:
        """从 JSON 加载（文件不存在则视为空）"""
        if not self._path.exists():
            self._data.clear()
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            # 文件损坏时直接清空，交给上层兜底
            self._data.clear()
            return

        data: dict[str, ScheduleData] = {}
        for date_str, item in raw.items():
            if not isinstance(item, dict):
                continue
            try:
                parsed = ScheduleData.from_dict(item, date=str(date_str))
                if parsed.date:
                    data[parsed.date] = parsed
            except Exception:
                continue

        self._data = data

    def _load_history(self) -> None:
        if not self._history_path.exists():
            self._history.clear()
            return
        try:
            raw = json.loads(self._history_path.read_text(encoding="utf-8"))
        except Exception:
            self._history.clear()
            return
        history: dict[str, list[ScheduleData]] = {}
        if isinstance(raw, dict):
            for date_str, items in raw.items():
                if not isinstance(items, list):
                    continue
                parsed_items: list[ScheduleData] = []
                for item in items[-10:]:
                    if not isinstance(item, dict):
                        continue
                    try:
                        data = ScheduleData.from_dict(item, date=str(date_str))
                    except Exception:
                        continue
                    if data.date:
                        parsed_items.append(data)
                if parsed_items:
                    history[str(date_str)] = parsed_items
        self._history = history

    def save(self) -> None:
        """保存为 JSON（原子写）"""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = self._path.with_suffix(".tmp")
        payload = {date: asdict(data) for date, data in self._data.items()}

        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)

    def _save_history(self) -> None:
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._history_path.with_suffix(".tmp")
        payload = {
            date: [asdict(item) for item in items[-10:]]
            for date, items in self._history.items()
            if items
        }
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._history_path)

    # ---------- 工具方法 ----------

    def clear(self, *, save: bool = True) -> None:
        """清空所有数据"""
        self._data.clear()
        if save:
            self.save()
