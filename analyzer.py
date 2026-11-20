"""Разбор подозрительного сообщения на «крючки» мошенников (без интернета и нейросетей)."""
import re

LINK_RE = re.compile(
    r"(https?://|www\.|\b[a-z0-9-]+\.(ru|рф|com|net|org|site|online|xyz|top|info|club|shop|pro|io|me|cc|click|link)\b)",
    re.IGNORECASE,
)
CARD_RE = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def analyze(text: str, hooks: dict) -> dict:
    """Возвращает {'found': [(hook_id, [совпадения])], 'score': int, 'level': 0..3}."""
    t = _norm(text)
    found = {}
    for hid, h in hooks.items():
        hits = []
        for m in h["markers"]:
            if _norm(m) in t and m not in hits:
                hits.append(m)
        # «срочн» и «срочно» — одно и то же: оставляем самое длинное совпадение
        hits = [m for m in hits if not any(m != o and _norm(m) in _norm(o) for o in hits)]
        if hits:
            found[hid] = hits
    if LINK_RE.search(text):
        found.setdefault("link", []).append("ссылка в тексте")
    if CARD_RE.search(text):
        found.setdefault("code", []).append("номер карты")

    score = sum(hooks[h]["weight"] for h in found)
    level = 0
    if score >= 1:
        level = 1
    if score >= 4:
        level = 2
    if score >= 7:
        level = 3
    # Код/данные + давление — почти всегда мошенничество
    if "code" in found and ({"urgency", "authority", "secrecy", "fear"} & set(found)):
        level = max(level, 2)
    if "secrecy" in found and ("code" in found or "greed" in found):
        level = 3
    ordered = sorted(found.items(), key=lambda kv: -hooks[kv[0]]["weight"])
    return {"found": ordered, "score": score, "level": level}


VERDICTS = [
    "🟢 Явных признаков мошенничества не нашёл.\nНо если у тебя просят деньги или коды либо торопят, всё равно проверь через другой канал.",
    "🟡 Есть подозрительные признаки. Будь осторожен.",
    "🟠 Похоже на мошенничество!",
    "🔴 Очень высокий риск! Это типичная схема мошенников.",
]


def format_report(result: dict, hooks: dict) -> str:
    lines = ["🔍 **Результат проверки**", "", VERDICTS[result["level"]]]
    if result["found"]:
        lines += ["", "🪝 **Найденные крючки:**"]
        for hid, hits in result["found"]:
            h = hooks[hid]
            sample = ", ".join(f"«{x}»" for x in hits[:3])
            lines.append(f"{h['emoji']} {h['title']}: {sample}")
        top = hooks[result["found"][0][0]]
        lines += ["", f"🛡️ **Что делать:** {top['defense']}"]
    return "\n".join(lines)
