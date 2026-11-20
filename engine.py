"""Логика тренажёра «Фейковый друг». Ничего не знает про мессенджер: получает события, возвращает сообщения.

Тот же движок работает и в MAX (main.py), и в консоли (python main.py --console).
"""
import json
import os
import random
import threading
from dataclasses import dataclass, field

from analyzer import analyze, format_report
import ai as ai_mod

BASE = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Out:
    text: str
    buttons: list = field(default_factory=list)  # [[(текст, payload), ...], ...]


MENU_BTN = [("🏠 Меню", "menu")]

RANKS = [(0, "🐣 Новичок"), (20, "👀 Внимательный"), (60, "🕵️ Детектор обмана"), (100, "🛡️ Киберстраж")]


def rank_for(score):
    r = RANKS[0][1]
    for th, name in RANKS:
        if score >= th:
            r = name
    return r


class Store:
    """Прогресс пользователей в data/users.json."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        try:
            with open(path, encoding="utf-8") as f:
                self.users = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.users = {}

    def get(self, uid, name=None):
        with self.lock:
            u = self.users.setdefault(str(uid), {"name": name or "Игрок", "best": {}, "wins": 0, "losses": 0,
                                                  "hooked_by": {}, "ai_wins": 0, "ai_losses": 0, "checks": 0})
            if name:
                u["name"] = name
            return u

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.users, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    @staticmethod
    def score(u):
        return sum(u["best"].values()) + 15 * u.get("ai_wins", 0)


class Engine:
    def __init__(self, ai_provider=None, data_path=None):
        with open(os.path.join(BASE, "scenarios.json"), encoding="utf-8") as f:
            self.sc = json.load(f)
        with open(os.path.join(BASE, "hooks.json"), encoding="utf-8") as f:
            self.hooks = json.load(f)
        self.store = Store(data_path or os.path.join(BASE, "data", "users.json"))
        self.ai = ai_provider
        self.sessions = {}  # uid -> {"mode": ..., ...}

    # ---------- входные события ----------

    def on_start(self, uid, name):
        self.store.get(uid, name)
        self.sessions[uid] = {"mode": "idle"}
        return self.menu(uid, first=True)

    def on_text(self, uid, name, text):
        text = (text or "").strip()
        s = self.sessions.setdefault(uid, {"mode": "idle"})
        low = text.lower()
        if low in ("/start", "start", "старт", "/menu", "меню", "/help", "помощь", "начать"):
            self.sessions[uid] = {"mode": "idle"}
            return self.menu(uid, first=low in ("/start", "start", "старт", "начать"))
        if low in ("/check", "проверить"):
            return self.cb_check(uid)
        if low in ("/stop", "стоп"):
            self.sessions[uid] = {"mode": "idle"}
            return [Out("Остановил. Возвращаемся в меню.", [MENU_BTN])]

        if s["mode"] == "check":
            s["mode"] = "idle"
            return self.do_check(uid, name, text)
        if s["mode"] == "ai":
            return self.ai_turn(uid, name, text)
        if s["mode"] == "scenario":
            step = self._step(s)
            return [Out("В тренировке отвечай кнопками 👇\nХочешь свободную переписку? Попробуй «🤖 Бой с ИИ-мошенником».",
                        self._option_buttons(s, step))]
        # Свободный текст в меню: если похоже на пересланное сообщение — сразу проверим
        if len(text) >= 25:
            return self.do_check(uid, name, text)
        return self.menu(uid)

    def on_callback(self, uid, name, payload):
        """Возвращает (подпись к нажатому сообщению или None, [Out])."""
        self.store.get(uid, name)
        s = self.sessions.setdefault(uid, {"mode": "idle"})
        p = payload.split(":")
        cmd = p[0]
        if cmd == "a":
            return self.answer(uid, p[1], p[2], int(p[3]))
        if s.get("mode") == "ai" and cmd != "ai_stop":
            s["mode"] = "idle"  # вышли из боя кнопкой
        if cmd == "menu":
            self.sessions[uid] = {"mode": "idle"}
            return None, self.menu(uid)
        if cmd == "play":
            return None, self.list_scenarios(uid)
        if cmd == "rand":
            return None, self.start_scenario(uid, self._pick_next(uid))
        if cmd == "sc":
            return None, self.start_scenario(uid, p[1])
        if cmd == "hooks":
            return None, self.list_hooks()
        if cmd == "hk":
            return None, self.show_hook(p[1])
        if cmd == "check":
            return None, self.cb_check(uid)
        if cmd == "me":
            return None, self.profile(uid)
        if cmd == "top":
            return None, self.top()
        if cmd == "ai":
            return None, self.ai_start(uid)
        if cmd == "ai_stop":
            self.sessions[uid] = {"mode": "idle"}
            return "⏹ Бой остановлен", self.menu(uid)
        return None, self.menu(uid)

    # ---------- меню ----------

    def menu(self, uid, first=False):
        head = (
            "🛡️ **Фейковый друг** — тренажёр против интернет-мошенников.\n\n"
            "Тебе будут писать «друзья», «банки» и «модераторы». Твоя задача — вовремя понять, что это обман. "
            "Ошибёшься — я покажу, на какой крючок тебя поймали.\n\n"
        ) if first else ""
        rows = [
            [("🎮 Тренировка", "play"), ("🎲 Случайная ситуация", "rand")],
            [("🤖 Бой с ИИ-мошенником", "ai")],
            [("🔍 Проверить сообщение", "check")],
            [("🪝 Крючки мошенников", "hooks"), ("🏆 Мой профиль", "me")],
        ]
        return [Out(head + "Выбирай, с чего начнём 👇", rows)]

    def list_scenarios(self, uid):
        u = self.store.get(uid)
        rows = []
        for sid, sc in self.sc.items():
            mark = "✅ " if sid in u["best"] else ""
            stars = "⭐" * sc["difficulty"]
            rows.append([(f"{mark}{sc['emoji']} {sc['title']} {stars}", f"sc:{sid}")])
        rows.append(MENU_BTN)
        done = len(u["best"])
        return [Out(f"🎮 **Выбери ситуацию**\nПройдено: {done} из {len(self.sc)}. Звёзды — сложность.", rows)]

    def _pick_next(self, uid):
        u = self.store.get(uid)
        left = [k for k in self.sc if k not in u["best"]]
        return random.choice(left or list(self.sc))

    # ---------- сценарии ----------

    def _step(self, s):
        return self.sc[s["sid"]]["steps"][s["step"]]

    def _option_buttons(self, s, step):
        return [[(o["text"], f"a:{s['sid']}:{s['step']}:{i}")] for i, o in enumerate(step["options"])]

    def _render_step(self, s):
        sc, step = self.sc[s["sid"]], self._step(s)
        who = step.get("who", sc["who"])
        outs = []
        if step.get("narration"):
            outs.append(Out(step["narration"]))
        for m in step.get("messages", []):
            outs.append(Out(f"💬 **{who}:**\n{m}"))
        s["seen_hooks"].extend(h for h in step.get("hooks", []) if h not in s["seen_hooks"])
        outs[-1].buttons = self._option_buttons(s, step)
        return outs

    def start_scenario(self, uid, sid):
        if sid not in self.sc:
            return self.list_scenarios(uid)
        sc = self.sc[sid]
        s = {"mode": "scenario", "sid": sid, "step": sc["start"], "depth": 0, "seen_hooks": []}
        self.sessions[uid] = s
        return [Out(f"{sc['emoji']} **{sc['title']}**\n\n🎭 {sc['intro']}\n\nЧитай внимательно и выбери ответ.")] + self._render_step(s)

    def answer(self, uid, sid, step_id, idx):
        s = self.sessions.get(uid, {})
        if s.get("mode") != "scenario" or s.get("sid") != sid or s.get("step") != step_id:
            return "⌛ Эта кнопка уже неактуальна", []
        step = self._step(s)
        if not 0 <= idx < len(step["options"]):
            return None, []
        opt = step["options"][idx]
        label = f"👉 Ты: {opt['text']}"
        if opt["result"] == "next":
            s["step"], s["depth"] = opt["goto"], s["depth"] + 1
            outs = [Out(f"💭 {opt['comment']}")] if opt.get("comment") else []
            return label, outs + self._render_step(s)
        return label, self._finish(uid, s, opt)

    def _finish(self, uid, s, opt):
        sc = self.sc[s["sid"]]
        u = self.store.get(uid)
        self.sessions[uid] = {"mode": "idle"}
        if opt["result"] == "safe":
            pts = 10 + 5 * sc["difficulty"] + (5 if s["depth"] == 0 else 0)
            first_time = s["sid"] not in u["best"]
            u["best"][s["sid"]] = max(u["best"].get(s["sid"], 0), pts)
            u["wins"] += 1
            bonus = " (+5 за мгновенную реакцию ⚡)" if s["depth"] == 0 else ""
            res = f"✅ **Ты не попался!**\n\n{opt['explain']}\n\n🏅 +{pts} очков{bonus}"
            if not first_time:
                res += "\n(в зачёт идёт лучший результат по ситуации)"
        else:
            hk = self.hooks.get(opt.get("hook"), {})
            u["losses"] += 1
            u["hooked_by"][opt.get("hook", "?")] = u["hooked_by"].get(opt.get("hook", "?"), 0) + 1
            res = (f"🎣 **Тебя поймали на крючок: {hk.get('emoji', '')} {hk.get('title', '')}**\n\n{opt['explain']}"
                   f"\n\n🛡️ **Как защититься:** {hk.get('defense', '')}")
        self.store.save()
        flags = "\n".join(f"🚩 {f}" for f in sc["red_flags"])
        used = ", ".join(f"{self.hooks[h]['emoji']} {self.hooks[h]['title']}" for h in s["seen_hooks"] if h in self.hooks)
        summary = f"**Разбор ситуации**\n\n{flags}\n\n🪝 **Приёмы мошенника:** {used}"
        rows = [[("▶️ Следующая ситуация", "rand"), ("🔁 Ещё раз", f"sc:{s['sid']}")], MENU_BTN]
        return [Out(res), Out(summary, rows)]

    # ---------- крючки ----------

    def list_hooks(self):
        items = list(self.hooks.items())
        rows = []
        for i in range(0, len(items), 2):
            rows.append([(f"{h['emoji']} {h['title']}", f"hk:{hid}") for hid, h in items[i:i + 2]])
        rows.append(MENU_BTN)
        return [Out("🪝 **Крючки мошенников**\nЭти приёмы социальной инженерии используют в переписке. Нажми, чтобы узнать подробнее.", rows)]

    def show_hook(self, hid):
        h = self.hooks.get(hid)
        if not h:
            return self.list_hooks()
        txt = f"{h['emoji']} **{h['title']}**\n\n{h['short']}\n\n🧠 **Как это работает:** {h['how']}\n\n🛡️ **Как защититься:** {h['defense']}"
        return [Out(txt, [[("⬅️ Все крючки", "hooks")], MENU_BTN])]

    # ---------- проверка сообщения ----------

    def cb_check(self, uid):
        self.sessions[uid] = {"mode": "check"}
        return [Out("🔍 Перешли мне подозрительное сообщение или вставь его текст. Я разберу его на крючки.", [MENU_BTN])]

    def do_check(self, uid, name, text):
        u = self.store.get(uid, name)
        u["checks"] = u.get("checks", 0) + 1
        self.store.save()
        report = format_report(analyze(text, self.hooks), self.hooks)
        if self.ai:
            try:
                report += "\n\n🤖 **Мнение нейросети:** " + ai_mod.explain_message(self.ai, text)
            except Exception:
                pass
        return [Out(report, [[("🔍 Проверить ещё", "check")], MENU_BTN])]

    # ---------- профиль ----------

    def profile(self, uid):
        u = self.store.get(uid)
        sc = Store.score(u)
        lines = [f"🏆 **Профиль: {u['name']}**", "", f"Звание: {rank_for(sc)}", f"Очки бдительности: {sc}",
                 f"Ситуаций пройдено: {len(u['best'])} из {len(self.sc)}",
                 f"Раскусил мошенника: {u['wins']} раз · попался: {u['losses']} раз"]
        if u.get("ai_wins") or u.get("ai_losses"):
            lines.append(f"Бои с ИИ: побед {u.get('ai_wins', 0)} · поражений {u.get('ai_losses', 0)}")
        nxt = [th for th, _ in RANKS if th > sc]
        if nxt:
            lines.append(f"До следующего звания: {nxt[0] - sc} очков")
        rows = [[("🏅 Рейтинг", "top"), ("🎲 Тренироваться", "rand")]]
        weak = sorted(u["hooked_by"].items(), key=lambda kv: -kv[1])
        if weak and weak[0][0] in self.hooks:
            h = self.hooks[weak[0][0]]
            lines += ["", f"⚠️ Чаще всего тебя ловят на крючок **{h['emoji']} {h['title']}**. Почитай, как от него защищаться."]
            rows.insert(0, [(f"{h['emoji']} Про «{h['title']}»", f"hk:{weak[0][0]}")])
        rows.append(MENU_BTN)
        return [Out("\n".join(lines), rows)]

    def top(self):
        users = sorted(self.store.users.values(), key=lambda u: -Store.score(u))[:10]
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🏅 **Рейтинг бдительности**", ""]
        for i, u in enumerate(users):
            m = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{m} {u['name']} — {Store.score(u)} ({rank_for(Store.score(u))})")
        if not users:
            lines.append("Пока пусто. Стань первым!")
        return [Out("\n".join(lines), [[("🏆 Мой профиль", "me")], MENU_BTN])]

    # ---------- бой с ИИ ----------

    def ai_start(self, uid):
        if not self.ai:
            return [Out("🤖 Режим с нейросетью не подключён.\n\nОрганизатору: добавьте ключ GigaChat или YandexGPT в config.json (см. README). "
                        "А пока попробуй тренировку на готовых ситуациях!", [[("🎮 Тренировка", "play")], MENU_BTN])]
        duel = ai_mod.ScamDuel(self.ai)
        try:
            turn = duel.opening()
        except ai_mod.AIError:
            return [Out("😵 Нейросеть сейчас недоступна. Попробуй позже или потренируйся на готовых ситуациях.",
                        [[("🎮 Тренировка", "play")], MENU_BTN])]
        self.sessions[uid] = {"mode": "ai", "duel": duel}
        intro = ("🤖 **Бой с ИИ-мошенником**\n\nСейчас тебе напишет нейросеть в роли мошенника. Сюжет каждый раз новый. "
                 "Отвечай своими словами, как в обычной переписке. Раскусишь обман — победа, отдашь код или деньги — поражение.")
        return [Out(intro), Out(f"💬 **Незнакомец:**\n{turn.get('reply', '…')}", [[("⏹ Сдаться и выйти", "ai_stop")]])]

    def ai_turn(self, uid, name, text):
        s = self.sessions[uid]
        duel = s["duel"]
        try:
            turn = duel.reply(text)
        except ai_mod.AIError:
            return [Out("😵 Нейросеть не ответила. Напиши ещё раз или выйди в меню.", [[("⏹ Выйти", "ai_stop")]])]
        u = self.store.get(uid, name)
        st = turn["status"]
        if st == "continue" and duel.turns >= 8:
            st, turn["explain"] = "safe", "Ты продержался 8 ходов и ничего не отдал. Мошенник сдался!"
        if st == "continue":
            return [Out(f"💬 **Незнакомец:**\n{turn.get('reply', '…')}", [[("⏹ Сдаться и выйти", "ai_stop")]])]
        self.sessions[uid] = {"mode": "idle"}
        rows = [[("🤖 Ещё бой", "ai")], MENU_BTN]
        if st == "safe":
            u["ai_wins"] = u.get("ai_wins", 0) + 1
            self.store.save()
            return [Out(f"✅ **Победа! Ты раскусил ИИ-мошенника.**\n\n{turn.get('explain', '')}\n\n🏅 +15 очков", rows)]
        u["ai_losses"] = u.get("ai_losses", 0) + 1
        hid = turn.get("hook")
        if hid in self.hooks:
            u["hooked_by"][hid] = u["hooked_by"].get(hid, 0) + 1
        self.store.save()
        hk = self.hooks.get(hid)
        head = f"🎣 **Попался! Крючок: {hk['emoji']} {hk['title']}**" if hk else "🎣 **Попался!**"
        defense = f"\n\n🛡️ **Как защититься:** {hk['defense']}" if hk else ""
        return [Out(f"{head}\n\n{turn.get('explain', '')}{defense}", rows)]
