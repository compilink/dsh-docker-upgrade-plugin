"""Verify a DSH application's privacy-preserving inventory, including fixed-cut session histories."""
import re


HASH = re.compile(r"[0-9a-f]{64}")


class InventoryError(ValueError):
    """Safe to display. No content, local names, routes or session IDs are included."""


def hashed(value):
    return isinstance(value, str) and bool(HASH.fullmatch(value))


def index(inventory):
    if not isinstance(inventory, dict) or inventory.get("schema") != 1 or not hashed(inventory.get("defaultModel")):
        raise InventoryError("DSH 业务验收清单无效。")
    providers = inventory.get("providers")
    rows = inventory.get("sessions")
    if (not isinstance(providers, list) or not all(hashed(item) for item in providers)
            or len(providers) != len(set(providers)) or not isinstance(rows, list)):
        raise InventoryError("无法验证 DSH 提供商或会话目录。")
    sessions = {}
    for row in rows:
        if not isinstance(row, dict):
            raise InventoryError("无法验证 DSH 会话历史。")
        sid, cursor, events = row.get("id"), row.get("cursor"), row.get("events")
        if (not hashed(sid) or type(cursor) is not int or cursor < -1 or cursor >= 2**53 - 1
                or type(events) is not int or events != cursor + 1 or not hashed(row.get("history")) or sid in sessions):
            raise InventoryError("无法验证 DSH 会话历史。")
        sessions[sid] = (cursor, row["history"])
    return sessions


def cuts(inventory):
    """Return per-session cursor cuts for the next signed application read."""
    return {sid: row[0] for sid, row in index(inventory).items()}


def assert_preserved(baseline, observed):
    """Require the old default, all old providers and every complete old event prefix."""
    old, new = index(baseline), index(observed)
    if baseline["defaultModel"] != observed["defaultModel"]:
        raise InventoryError("DSH 默认提供商、模型或推理设置发生变化。")
    if not set(baseline["providers"]).issubset(observed["providers"]):
        raise InventoryError("已注册的 DSH 模型提供商缺失。")
    if any(new.get(sid) != row for sid, row in old.items()):
        raise InventoryError("已有 DSH 会话缺失，或其已保存的完整事件历史发生变化。")
