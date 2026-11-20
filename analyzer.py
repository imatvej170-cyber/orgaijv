"""Разбор подозрительного сообщения на «крючки» мошенников (без интернета и нейросетей)."""
import re

LINK_RE = re.compile(
    r"(https?://|www\.|\b[a-z0-9-]+\.(ru|рф|com|net|org|site|online|xyz|top|info|club|shop|pro|io|me|cc|click|link)\b)",
    re.IGNORECASE,
)
DOMAIN_RE = re.compile(r"(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+[a-z]{2,}|(?:[а-я0-9-]+\.)+рф)", re.IGNORECASE)
CARD_RE = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")

# Настоящие адреса популярных сервисов: всё похожее, но другое — подозрительно
BRANDS = {
    "gosuslugi": "gosuslugi.ru", "sberbank": "sberbank.ru", "sber": "sberbank.ru", "tbank": "tbank.ru",
    "tinkoff": "tbank.ru", "vtb": "vtb.ru", "alfabank": "alfabank.ru", "ozon": "ozon.ru",
    "wildberries": "wildberries.ru", "avito": "avito.ru", "max": "max.ru", "vkontakte": "vk.com",
    "pochta": "pochta.ru", "sfr": "sfr.gov.ru", "nalog": "nalog.gov.ru", "mosru": "mos.ru",
    "steam": "steampowered.com", "roblox": "roblox.com", "brawlstars": "supercell.com", "yandex": "yandex.ru",
}
OFFICIAL = set(BRANDS.values()) | {"vk.com", "mos.ru", "ya.ru", "gov.ru", "steamcommunity.com", "sber.ru"}
SHORTENERS = {"clck.ru", "bit.ly", "tinyurl.com", "cutt.ly", "goo.su", "t.ly", "is.gd", "vk.cc", "u.to", "rebrand.ly"}
ODD_TLDS = {"top", "xyz", "site", "online", "click", "link", "shop", "icu", "buzz", "live", "info", "cc", "monster", "store"}


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def _lev1(a: str, b: str) -> bool:
    """Отличаются не больше чем на одну букву (замена, вставка или удаление)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    return any(a == b[:i] + b[i + 1:] for i in range(len(b)))


def check_links(text: str):
    """Возвращает [(домен, проблема)] для подозрительных ссылок."""
    issues = []
    for dom in {d.lower().rstrip(".") for d in DOMAIN_RE.findall(text)}:
        if any(dom == o or dom.endswith("." + o) for o in OFFICIAL):
            continue
        if dom in SHORTENERS:
            issues.append((dom, "сокращённая ссылка: не видно, куда она ведёт"))
            continue
        if "xn--" in dom:
            issues.append((dom, "адрес с подменой букв (похожие символы из других алфавитов)"))
        parts = re.split(r"[.-]", dom.rsplit(".", 1)[0])
        fake = None
        for p in parts:
            for brand, official in BRANDS.items():
                if p == brand or (len(brand) >= 5 and _lev1(p, brand)):
                    fake = official
        if fake:
            issues.append((dom, f"похоже на подделку адреса {fake}"))
        tld = dom.rsplit(".", 1)[-1]
        if tld in ODD_TLDS:
            issues.append((dom, f"необычная доменная зона .{tld}"))
        if dom.replace("xn--", "").count("-") >= 2:
            issues.append((dom, "длинный адрес с дефисами, как у одноразовых сайтов"))
    return issues


def analyze(text: str, hooks: dict) -> dict:
    """Возвращает {'found': [(hook_id, [совпадения])], 'links': [...], 'score': int, 'level': 0..3}."""
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
    links = check_links(text)

    score = sum(hooks[h]["weight"] for h in found) + 2 * len(links)
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
    if "secrecy" in found and ("code" in found or "greed" in found or "money" in found):
        level = 3
    if any("подделку" in why or "подменой" in why for _, why in links):
        level = max(level, 2)
    ordered = sorted(found.items(), key=lambda kv: -hooks[kv[0]]["weight"])
    return {"found": ordered, "links": links, "score": score, "level": level}


VERDICTS = [
    ("🟢 Явных признаков мошенничества не нашёл.\nНо если у тебя просят деньги или коды либо торопят, всё равно проверь через другой канал.",
     "🟢 Явных признаков мошенничества не нашёл.\nНо если у вас просят деньги или коды либо торопят, всё равно проверьте через другой канал."),
    ("🟡 Есть подозрительные признаки. Будь осторожен.", "🟡 Есть подозрительные признаки. Будьте осторожны."),
    ("🟠 Похоже на мошенничество!", "🟠 Похоже на мошенничество!"),
    ("🔴 Очень высокий риск! Это типичная схема мошенников.", "🔴 Очень высокий риск! Это типичная схема мошенников."),
]


def format_report(result: dict, hooks: dict, formal=False) -> str:
    lines = ["🔍 **Результат проверки**", "", VERDICTS[result["level"]][1 if formal else 0]]
    if result["found"]:
        lines += ["", "🪝 **Найденные крючки:**"]
        for hid, hits in result["found"]:
            h = hooks[hid]
            sample = ", ".join(f"«{x}»" for x in hits[:3])
            lines.append(f"{h['emoji']} {h['title']}: {sample}")
    if result.get("links"):
        lines += ["", "🔗 **Проверка ссылок:**"]
        for dom, why in result["links"][:5]:
            lines.append(f"⚠️ {dom} — {why}")
    if result["found"]:
        top = hooks[result["found"][0][0]]
        lines += ["", f"🛡️ **Что делать:** {top['defense_v' if formal else 'defense']}"]
    return "\n".join(lines)
