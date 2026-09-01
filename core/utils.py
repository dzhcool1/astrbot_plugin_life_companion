import datetime
import re
from typing import Any


def time_desc(h=None):
    """返回中文时段：深夜/清晨/上午/中午/下午/晚上"""
    h = (datetime.datetime.now().hour if h is None else h) % 24
    return (
        "深夜"
        if h < 6
        else "清晨"
        if h < 9
        else "上午"
        if h < 12
        else "中午"
        if h < 14
        else "下午"
        if h < 18
        else "晚上"
        if h < 22
        else "深夜"
    )

def parse_schedule_time(schedule_time: str | None) -> tuple[int, int]:
    schedule_time = str(schedule_time or "00:00")
    try:
        hour, minute = map(int, schedule_time.split(":", 1))
    except Exception:
        return 0, 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return 0, 0


def resolve_business_now(
    schedule_time: str | None,
    now: datetime.datetime | None = None,
) -> datetime.datetime:
    now = now or datetime.datetime.now()
    hour, minute = parse_schedule_time(schedule_time)
    boundary = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < boundary:
        return now - datetime.timedelta(days=1)
    return now


_SCHEDULE_TIME_RE = re.compile(
    r"(?m)^\s*(?:[-*•·]\s*)?"
    r"(?:\d+[.)、]\s*)?"
    r"(?:[^\d\n]{0,12}?\s*)?"
    r"(?P<hour>[01]?\d|2[0-3])"
    r"(?:[:：](?P<minute>[0-5]?\d)|点(?:(?P<half>半)|(?P<minute_cn>[0-5]?\d)?分?))"
    r"\s*(?P<text>.+?)\s*$"
)


def extract_schedule_activities(schedule: str) -> list[tuple[int, str]]:
    """Extract time-ordered schedule entries as (minute_of_day, text)."""
    activities: list[tuple[int, str]] = []
    for match in _SCHEDULE_TIME_RE.finditer(str(schedule or "")):
        hour = int(match.group("hour"))
        minute = 30 if match.group("half") else int(
            match.group("minute") or match.group("minute_cn") or "0"
        )
        text = match.group("text").strip()
        if not text:
            continue
        activities.append((hour * 60 + minute, f"{hour:02d}:{minute:02d} {text}"))
    return sorted(activities, key=lambda item: item[0])


def extract_timeline_activities(
    timeline: list[dict[str, Any]] | None,
) -> list[tuple[int, str]]:
    """Convert structured timeline entries to the activity format used by injection."""
    activities: list[tuple[int, str]] = []
    for item in timeline or []:
        if not isinstance(item, dict):
            continue
        match = re.match(r"^\s*([01]?\d|2[0-3])[:：]([0-5]?\d)\s*$", str(item.get("time", "")))
        activity = str(item.get("activity", "") or "").strip()
        if not match or not activity:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        activities.append((hour * 60 + minute, f"{hour:02d}:{minute:02d} {activity}"))
    return sorted(activities, key=lambda item: item[0])


def timeline_to_text(timeline: list[dict[str, Any]] | None) -> str:
    """Render structured entries for prompts and human-readable replies."""
    lines: list[str] = []
    for item in timeline or []:
        if not isinstance(item, dict):
            continue
        time_value = str(item.get("time", "") or "").strip()
        activity = str(item.get("activity", "") or "").strip()
        if not time_value or not activity:
            continue
        location = str(item.get("location", "") or "").strip()
        suffix = f"（{location}）" if location else ""
        lines.append(f"{time_value} {activity}{suffix}")
    return "\n".join(lines)


def select_current_activity(
    schedule: str,
    now: datetime.datetime | None = None,
    *,
    timeline: list[dict[str, Any]] | None = None,
    wrap_previous_day: bool = False,
) -> str:
    """Return the latest scheduled activity at or before now, or the next one."""
    activities = extract_timeline_activities(timeline) or extract_schedule_activities(schedule)
    if not activities:
        return ""

    now = now or datetime.datetime.now()
    current_minute = now.hour * 60 + now.minute
    if current_minute < activities[0][0]:
        return activities[-1][1] if wrap_previous_day else activities[0][1]

    current = activities[0][1]
    for minute, text in activities:
        if minute <= current_minute:
            current = text
        else:
            break
    return current


def build_character_state_injection(
    outfit: str,
    schedule: str,
    *,
    timeline: list[dict[str, Any]] | None = None,
    now: datetime.datetime | None = None,
    business_now: datetime.datetime | None = None,
) -> str:
    """Build the system prompt fragment injected into normal LLM requests."""
    now = now or datetime.datetime.now()
    business_now = business_now or now
    current_activity = select_current_activity(
        schedule,
        now=now,
        timeline=timeline,
        wrap_previous_day=business_now.date() < now.date(),
    )
    current_state = current_activity or "未解析到具体时间点，请按今日日程整体保持一致"
    timeline_text = timeline_to_text(timeline)
    timeline_block = f"\n时间线:\n{timeline_text}" if timeline_text else ""

    return f"""
<character_state>
时间: {time_desc(now.hour)}
穿着: {outfit}
当前状态: {current_state}
今日日程: {schedule}
{timeline_block}
</character_state>
<state_following_rules>
- 当用户问到正在做什么、今天安排、所在场景、穿着或生活状态时，必须以 <character_state> 为准。
- 不得编造与当前状态或今日日程冲突的上课、上班、外出、睡觉等状态。
- 与用户问题无关时无需主动提及这些状态。
</state_following_rules>"""
