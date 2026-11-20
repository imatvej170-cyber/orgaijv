"""Логика тренажёра «Фейковый друг». Ничего не знает про мессенджер: получает события, возвращает сообщения.

Тот же движок работает и в MAX (main.py), и в консоли (python main.py --console).
"""
import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field

from analyzer import analyze, format_report
import ai as ai_mod

BASE = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Out:
    text: str
    buttons: list = field(default_factory=list)  # [[(текст, payload), ...], ...]
    typing: bool = False  # перед сообщением показать «печатает…» (реплики мошенника)


MENU_BTN = [("🏠 Меню", "menu")]
RANKS = [(0, "🐣 Новичок"), (20, "👀 Внимательный"), (60, "🕵️ Детектор обмана"), (100, "🛡️ Киберстраж")]
AUDIENCES = {"teen": "🧑‍🎓 Школьник", "adult": "🧑 Взрослый", "senior": "👵 Пенсионер"}
CITIES = ["Новосибирск", "Екатеринбург", "Казань", "Владивосток", "Краснодар", "Омск", "Самара"]
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def rank_for(score):
    r = RANKS[0][1]
    for th, name in RANKS:
        if score >= th:
            r = name
    return r


def log_ai_error(e):
    print(time.strftime("%H:%M:%S"), "ОШИБКА ИИ:", e, "| подробная проверка: python check_ai.py", flush=True)


def fill(text, vars_):
    return re.sub(r"\{(\w+)\}", lambda m: str(vars_.get(m.group(1), m.group(0))), text)


class Store:
    """Прогресс пользователей и классы в data/users.json."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        if "users" not in data:  # старый формат: просто пользователи
            data = {"users": data, "classes": {}}
        self.users, self.classes = data["users"], data.get("classes", {})

    def get(self, uid, name=None):
        with self.lock:
            u = self.users.setdefault(str(uid), {"name": name or "Игрок", "created": int(time.time())})
            for k, v in {"aud": None, "best": {}, "wins": 0, "losses": 0, "paranoid": 0, "hooked_by": {},
                         "plays": {}, "ai_wins": 0, "ai_losses": 0, "checks": 0, "sos": 0,
                         "streak": 0, "max_streak": 0, "cls": None}.items():
                u.setdefault(k, v if not isinstance(v, dict) else {})
            if name:
                u["name"] = name
            return u

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"users": self.users, "classes": self.classes}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    @staticmethod
    def score(u):
        return sum(u["best"].values()) + 15 * u.get("ai_wins", 0)


class Engine:
    def __init__(self, ai_provider=None, data_path=None):
        def load(name):
            with open(os.path.join(BASE, name), encoding="utf-8") as f:
                return json.load(f)
        self.sc, self.hooks, self.sos = load("scenarios.json"), load("hooks.json"), load("sos.json")
        self.store = Store(data_path or os.path.join(BASE, "data", "users.json"))
        self.ai = ai_provider
        self.bot_username = None  # заполняется из main.py после запуска
        self.sessions = {}  # uid -> {"mode": ...}

    # ---------- «ты» или «вы» ----------

    def formal(self, u):
        return u.get("aud") == "senior"

    def T(self, u, ty, vy):
        return vy if self.formal(u) else ty

    # ---------- входные события ----------

    def on_start(self, uid, name, payload=None):
        u = self.store.get(uid, name)
        self.sessions[uid] = {"mode": "idle"}
        outs = []
        if payload and payload.lower().startswith("class_"):
            outs += self._join_class(uid, payload[6:])
        if not u["aud"]:
            return outs + self.ask_audience(first=True)
        return outs + self.menu(uid, first=True)

    def on_text(self, uid, name, text):
        text = (text or "").strip()
        u = self.store.get(uid, name)
        s = self.sessions.setdefault(uid, {"mode": "idle"})
        low = text.lower()
        if low in ("/start", "start", "старт", "начать"):
            return self.on_start(uid, name)
        if low in ("/menu", "меню", "/help", "помощь"):
            self.sessions[uid] = {"mode": "idle"}
            return self.menu(uid)
        if low in ("/check", "проверить"):
            return self.cb_check(uid)
        if low in ("/sos", "sos", "сос", "помогите"):
            return self.sos_menu(uid)
        if low in ("/class", "класс"):
            return self.class_menu(uid)
        if low == "/stats":
            return self.stats()
        if low in ("/stop", "стоп"):
            self.sessions[uid] = {"mode": "idle"}
            return [Out("Остановил. Возвращаемся в меню.", [MENU_BTN])]

        mode = s["mode"]
        if mode == "check":
            s["mode"] = "idle"
            return self.do_check(uid, name, text)
        if mode == "ai":
            return self.ai_turn(uid, name, text)
        if mode == "join":
            s["mode"] = "idle"
            return self._join_class(uid, text)
        if mode == "newclass":
            s["mode"] = "idle"
            return self._create_class(uid, text)
        if mode == "scenario":
            return [Out(self.T(u, "В тренировке отвечай кнопками 👇", "В тренировке отвечайте кнопками 👇"),
                        self._option_buttons(s, self._step(s)))]
        # Свободный текст в меню: если похоже на пересланное сообщение — сразу проверим
        if len(text) >= 25:
            return self.do_check(uid, name, text)
        if not u["aud"]:
            return self.ask_audience(first=True)
        return self.menu(uid)

    def on_callback(self, uid, name, payload):
        """Возвращает (подпись к нажатому сообщению или None, [Out])."""
        self.store.get(uid, name)
        s = self.sessions.setdefault(uid, {"mode": "idle"})
        p = payload.split(":")
        cmd = p[0]
        if cmd == "a":
            return self.answer(uid, p[1], p[2], int(p[3]))
        if s.get("mode") in ("ai", "join", "newclass", "check") and cmd != "ai_stop":
            s["mode"] = "idle"  # вышли из режима кнопкой
        routes = {
            "menu": lambda: self.menu(uid),
            "play": lambda: self.list_scenarios(uid, show_all=len(p) > 1),
            "rand": lambda: self.start_scenario(uid, self._pick_next(uid)),
            "sc": lambda: self.start_scenario(uid, p[1]),
            "hooks": lambda: self.list_hooks(),
            "hk": lambda: self.show_hook(uid, p[1]),
            "check": lambda: self.cb_check(uid),
            "me": lambda: self.profile(uid),
            "top": lambda: self.top(),
            "ai": lambda: self.ai_start(uid),
            "sos": lambda: self.sos_menu(uid) if len(p) == 1 else self.sos_show(uid, p[1]),
            "memo": lambda: self.memo(uid),
            "aud": lambda: self.set_audience(uid, p[1]) if len(p) > 1 else self.ask_audience(),
            "cls": lambda: self.class_menu(uid),
            "cls_join": lambda: self.cls_prompt(uid, "join"),
            "cls_new": lambda: self.cls_prompt(uid, "newclass"),
            "clsv": lambda: self.class_view(uid, p[1]),
            "cls_leave": lambda: self.cls_leave(uid),
        }
        if cmd == "menu":
            self.sessions[uid] = {"mode": "idle"}
        if cmd == "ai_stop":
            self.sessions[uid] = {"mode": "idle"}
            return "⏹ Бой остановлен", self.menu(uid)
        return None, routes.get(cmd, lambda: self.menu(uid))()

    # ---------- аудитория и меню ----------

    def ask_audience(self, first=False):
        head = ("🛡️ **Фейковый друг** — тренажёр, который учит распознавать интернет-мошенников.\n\n"
                if first else "")
        return [Out(head + "Кто вы? Я подберу ситуации, которые чаще всего случаются именно с вами.",
                    [[(v, f"aud:{k}")] for k, v in AUDIENCES.items()])]

    def set_audience(self, uid, aud):
        u = self.store.get(uid)
        if aud in AUDIENCES:
            u["aud"] = aud
            self.store.save()
        return self.menu(uid, first=True)

    def menu(self, uid, first=False):
        u = self.store.get(uid)
        if not u["aud"]:
            return self.ask_audience(first=True)
        head = ""
        if first:
            head = self.T(
                u,
                "Тебе будут писать «друзья», «банки» и «модераторы». Твоя задача — вовремя понять, что это обман. "
                "Ошибёшься — я покажу, на какой крючок тебя поймали. Но осторожно: иногда пишет и не мошенник 😉\n\n",
                "Вам будут писать «родственники», «банки» и «соцслужбы». Ваша задача — вовремя понять, что это обман. "
                "Если ошибётесь, я покажу, на какой крючок вас поймали. Но иногда пишет и не мошенник.\n\n")
        rows = [
            [("🎮 Тренировка", "play"), ("🎲 Случайная ситуация", "rand")],
            [("🤖 Бой с ИИ-мошенником", "ai")],
            [("🔍 Проверить сообщение", "check")],
            [("🆘 Меня уже обманули", "sos")],
            [("🪝 Крючки мошенников", "hooks"), ("🏆 Профиль", "me")],
            [("🏫 Класс", "cls"), ("📤 Памятка для близких", "memo")],
        ]
        return [Out(head + self.T(u, "Выбирай, с чего начнём 👇", "Выберите, с чего начнём 👇"), rows)]

    def _for_aud(self, u):
        return [k for k, sc in self.sc.items() if not u["aud"] or u["aud"] in sc.get("audience", AUDIENCES)]

    def list_scenarios(self, uid, show_all=False):
        u = self.store.get(uid)
        ids = list(self.sc) if show_all else self._for_aud(u)
        rows = []
        for sid in ids:
            sc = self.sc[sid]
            mark = "✅ " if sid in u["best"] else ""
            rows.append([(f"{mark}{sc['emoji']} {sc['title']} {'⭐' * sc['difficulty']}", f"sc:{sid}")])
        rows.append([("👤 Только мои ситуации", "play")] if show_all else [("📚 Все ситуации", "play:all")])
        rows.append(MENU_BTN)
        done = len([i for i in ids if i in u["best"]])
        head = self.T(u, "🎮 **Выбери ситуацию**", "🎮 **Выберите ситуацию**")
        return [Out(f"{head}\nПройдено: {done} из {len(ids)}. Звёзды — сложность.", rows)]

    def _pick_next(self, uid):
        u = self.store.get(uid)
        pool = self._for_aud(u)
        left = [k for k in pool if k not in u["best"]]
        return random.choice(left or pool)

    # ---------- сценарии ----------

    def _step(self, s):
        return self.sc[s["sid"]]["steps"][s["step"]]

    def _option_buttons(self, s, step):
        order = list(range(len(step["options"])))
        random.shuffle(order)  # правильный ответ каждый раз на новом месте
        return [[(fill(step["options"][i]["text"], s["vars"])[:128], f"a:{s['sid']}:{s['step']}:{i}")] for i in order]

    def _render_step(self, s):
        sc, step = self.sc[s["sid"]], self._step(s)
        who = fill(step.get("who", sc["who"]), s["vars"])
        outs = []
        if step.get("narration"):
            outs.append(Out(fill(step["narration"], s["vars"])))
        for m in step.get("messages", []):
            outs.append(Out(f"💬 **{who}:**\n{fill(m, s['vars'])}", typing=True))
        s["seen_hooks"].extend(h for h in step.get("hooks", []) if h not in s["seen_hooks"])
        outs[-1].buttons = self._option_buttons(s, step)
        return outs

    def start_scenario(self, uid, sid):
        if sid not in self.sc:
            return self.list_scenarios(uid)
        u = self.store.get(uid)
        sc = self.sc[sid]
        vars_ = {"code": str(random.randint(100000, 999999)), "code4": str(random.randint(1000, 9999)),
                 "city": random.choice(CITIES)}
        vars_.update({k: random.choice(v) for k, v in sc.get("vars", {}).items()})
        s = {"mode": "scenario", "sid": sid, "step": sc["start"], "depth": 0, "seen_hooks": [], "vars": vars_}
        self.sessions[uid] = s
        tail = self.T(u, "Читай внимательно и выбери ответ.", "Читайте внимательно и выберите ответ.")
        return [Out(f"{sc['emoji']} **{sc['title']}**\n\n🎭 {fill(sc['intro'], vars_)}\n\n{tail}")] + self._render_step(s)

    def answer(self, uid, sid, step_id, idx):
        s = self.sessions.get(uid, {})
        if s.get("mode") != "scenario" or s.get("sid") != sid or s.get("step") != step_id:
            return "⌛ Эта кнопка уже неактуальна", []
        step = self._step(s)
        if not 0 <= idx < len(step["options"]):
            return None, []
        opt = step["options"][idx]
        u = self.store.get(uid)
        label = f"👉 {self.T(u, 'Ты', 'Вы')}: {fill(opt['text'], s['vars'])}"
        if opt["result"] == "next":
            s["step"], s["depth"] = opt["goto"], s["depth"] + 1
            outs = [Out(f"💭 {fill(opt['comment'], s['vars'])}")] if opt.get("comment") else []
            return label, outs + self._render_step(s)
        return label, self._finish(uid, s, opt)

    def _finish(self, uid, s, opt):
        sid = s["sid"]
        sc = self.sc[sid]
        u = self.store.get(uid)
        self.sessions[uid] = {"mode": "idle"}
        explain = fill(opt["explain"], s["vars"])
        play = u["plays"].setdefault(sid, {"w": 0, "l": 0, "p": 0})
        extra_rows = []
        if opt["result"] == "safe":
            pts = 10 + 5 * sc["difficulty"] + (5 if s["depth"] == 0 else 0)
            first_time = sid not in u["best"]
            u["best"][sid] = max(u["best"].get(sid, 0), pts)
            u["wins"] += 1
            u["streak"] += 1
            u["max_streak"] = max(u["max_streak"], u["streak"])
            play["w"] += 1
            bonus = " (+5 за мгновенную реакцию ⚡)" if s["depth"] == 0 else ""
            head = self.T(u, "Ты не попался!", "Вы не попались!") if not sc.get("legit") else "Верно!"
            res = f"✅ **{head}**\n\n{explain}\n\n🏅 +{pts} очков{bonus}"
            if not first_time:
                res += "\n(в зачёт идёт лучший результат по ситуации)"
            if u["streak"] in (3, 5, 10):
                res += f"\n🔥 {u['streak']} побед подряд!"
        elif opt["result"] == "paranoid":
            u["paranoid"] += 1
            u["streak"] = 0
            play["p"] += 1
            res = f"🙈 **{self.T(u, 'Перестраховался!', 'Вы перестраховались!')}**\n\n{explain}"
        else:
            hid = opt.get("hook", "?")
            hk = self.hooks.get(hid, {})
            u["losses"] += 1
            u["streak"] = 0
            u["hooked_by"][hid] = u["hooked_by"].get(hid, 0) + 1
            play["l"] += 1
            caught = self.T(u, "Тебя поймали на крючок", "Вас поймали на крючок")
            defense = hk.get("defense_v" if self.formal(u) else "defense", "")
            res = (f"🎣 **{caught}: {hk.get('emoji', '')} {hk.get('title', '')}**\n\n{explain}"
                   f"\n\n🛡️ **Как защититься:** {defense}")
            extra_rows = [[("🆘 Что делать, если это случилось на самом деле", "sos")]]
        self.store.save()
        if sc.get("legit"):
            flags = "\n".join(f"✅ {f}" for f in sc.get("green_flags", []))
            summary = f"**Разбор ситуации**\n\nЭто был НЕ мошенник 👍 Признаки, что всё в порядке:\n\n{flags}"
        else:
            flags = "\n".join(f"🚩 {f}" for f in sc["red_flags"])
            used = ", ".join(f"{self.hooks[h]['emoji']} {self.hooks[h]['title']}" for h in s["seen_hooks"] if h in self.hooks)
            summary = f"**Разбор ситуации**\n\n{flags}\n\n🪝 **Приёмы мошенника:** {used}"
        rows = extra_rows + [[("▶️ Следующая ситуация", "rand"), ("🔁 Ещё раз", f"sc:{sid}")], MENU_BTN]
        return [Out(res), Out(summary, rows)]

    # ---------- крючки ----------

    def list_hooks(self):
        items = list(self.hooks.items())
        rows = []
        for i in range(0, len(items), 2):
            rows.append([(f"{h['emoji']} {h['title']}", f"hk:{hid}") for hid, h in items[i:i + 2]])
        rows.append(MENU_BTN)
        return [Out("🪝 **Крючки мошенников**\nЭти приёмы социальной инженерии используют в переписке. Нажмите, чтобы узнать подробнее.", rows)]

    def show_hook(self, uid, hid):
        h = self.hooks.get(hid)
        if not h:
            return self.list_hooks()
        u = self.store.get(uid)
        defense = h["defense_v"] if self.formal(u) else h["defense"]
        txt = f"{h['emoji']} **{h['title']}**\n\n{h['short']}\n\n🧠 **Как это работает:** {h['how']}\n\n🛡️ **Как защититься:** {defense}"
        return [Out(txt, [[("⬅️ Все крючки", "hooks")], MENU_BTN])]

    # ---------- проверка сообщения ----------

    def cb_check(self, uid):
        u = self.store.get(uid)
        self.sessions[uid] = {"mode": "check"}
        return [Out(self.T(u, "🔍 Перешли мне подозрительное сообщение или вставь его текст. Я разберу его на крючки и проверю ссылки.",
                           "🔍 Перешлите мне подозрительное сообщение или вставьте его текст. Я разберу его на крючки и проверю ссылки."),
                    [MENU_BTN])]

    def do_check(self, uid, name, text):
        u = self.store.get(uid, name)
        u["checks"] += 1
        self.store.save()
        res = analyze(text, self.hooks)
        report = format_report(res, self.hooks, formal=self.formal(u))
        if self.ai:
            try:
                report += "\n\n🤖 **Мнение нейросети:** " + ai_mod.explain_message(self.ai, text)
            except Exception as e:
                log_ai_error(e)
        rows = [[("🔍 Проверить ещё", "check")], MENU_BTN]
        if res["level"] >= 2:
            rows.insert(0, [("🆘 Я уже ответил(а) мошеннику", "sos")])
        return [Out(report, rows)]

    # ---------- «Меня уже обманули» ----------

    def sos_menu(self, uid):
        u = self.store.get(uid)
        u["sos"] += 1
        self.store.save()
        self.sessions[uid] = {"mode": "idle"}
        teen = "\n\n👨‍👩‍👧 Сразу расскажи родителям: вместе вы справитесь быстрее." if u["aud"] == "teen" else ""
        txt = ("🆘 **Если вас обманули**\n\nГлавное — действовать быстро и не стыдиться. Мошенники — профессионалы, "
               f"их жертвой может стать каждый.{teen}\n\nЧто случилось?")
        rows = [[(v["button"], f"sos:{k}")] for k, v in self.sos.items()] + [MENU_BTN]
        return [Out(txt, rows)]

    def sos_show(self, uid, key):
        item = self.sos.get(key)
        if not item:
            return self.sos_menu(uid)
        steps = "\n\n".join(f"{i}. {st}" for i, st in enumerate(item["steps"], 1))
        txt = f"🆘 **{item['title']}**\n\n{steps}"
        return [Out(txt, [[("⬅️ Другая ситуация", "sos")], MENU_BTN])]

    # ---------- памятка ----------

    def memo(self, uid):
        u = self.store.get(uid)
        where = f"\n\nПотренироваться распознавать обман: бот «Фейковый друг» в MAX — {self.bot_username}" if self.bot_username \
            else "\n\nПотренироваться распознавать обман: бот «Фейковый друг» в MAX."
        memo = ("🛡️ **5 правил против мошенников**\n\n"
                "1. Код из СМС, пароль и CVV не сообщайте никому — ни «банку», ни «полиции», ни «другу».\n"
                "2. Торопят и пугают — значит, обманывают. Возьмите паузу.\n"
                "3. Просят денег от имени родных — позвоните им сами по известному номеру.\n"
                "4. Не переходите по ссылкам из сообщений и не вводите там данные.\n"
                "5. Просят держать всё в секрете — сразу расскажите близким." + where)
        hint = self.T(u, "👆 Перешли это сообщение родным и друзьям: нажми на него и выбери «Переслать».",
                      "👆 Перешлите это сообщение родным и друзьям: нажмите на него и выберите «Переслать».")
        return [Out(memo), Out(hint, [MENU_BTN])]

    # ---------- профиль и достижения ----------

    def achievements(self, u):
        mine = self._for_aud(u)
        legit = [k for k in mine if self.sc[k].get("legit")]
        return [
            ("🎯", "Первая победа", u["wins"] >= 1),
            ("🔥", "5 побед подряд", u["max_streak"] >= 5),
            ("🧭", "Пройти все свои ситуации", bool(mine) and all(k in u["best"] for k in mine)),
            ("🧠", "Узнать настоящего друга", any(k in u["best"] for k in legit)),
            ("🤖", "Победить ИИ-мошенника", u["ai_wins"] >= 1),
            ("🔍", "Проверить 3 сообщения", u["checks"] >= 3),
            ("🛡️", "Звание «Киберстраж»", Store.score(u) >= 100),
        ]

    def profile(self, uid):
        u = self.store.get(uid)
        sc = Store.score(u)
        mine = self._for_aud(u)
        lines = [f"🏆 **Профиль: {u['name']}**", "",
                 f"Режим: {AUDIENCES.get(u['aud'], '—')}",
                 f"Звание: {rank_for(sc)}", f"Очки бдительности: {sc}",
                 f"Ситуаций пройдено: {len([k for k in mine if k in u['best']])} из {len(mine)}",
                 self.T(u, f"Раскусил мошенника: {u['wins']} · попался: {u['losses']} · перестраховался: {u['paranoid']}",
                        f"Распознали обман: {u['wins']} · попались: {u['losses']} · перестраховались: {u['paranoid']}")]
        if u["ai_wins"] or u["ai_losses"]:
            lines.append(f"Бои с ИИ: побед {u['ai_wins']} · поражений {u['ai_losses']}")
        nxt = [th for th, _ in RANKS if th > sc]
        if nxt:
            lines.append(f"До следующего звания: {nxt[0] - sc} очков")
        ach = self.achievements(u)
        got = sum(1 for *_, ok in ach if ok)
        lines += ["", f"🎖️ **Достижения: {got} из {len(ach)}**"]
        lines += [f"{e if ok else '▫️'} {t}" for e, t, ok in ach]
        rows = [[("🏅 Рейтинг", "top"), ("🎲 Тренироваться", "rand")]]
        weak = sorted(u["hooked_by"].items(), key=lambda kv: -kv[1])
        if weak and weak[0][0] in self.hooks:
            h = self.hooks[weak[0][0]]
            lines += ["", f"⚠️ Чаще всего {self.T(u, 'тебя', 'вас')} ловят на крючок **{h['emoji']} {h['title']}**."]
            rows.insert(0, [(f"{h['emoji']} Как защититься от «{h['title']}»", f"hk:{weak[0][0]}")])
        rows.append([("🔄 Сменить режим", "aud")])
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
            lines.append("Пока пусто. Станьте первым!")
        return [Out("\n".join(lines), [[("🏆 Профиль", "me")], MENU_BTN])]

    # ---------- классы (режим учителя) ----------

    def class_menu(self, uid):
        u = self.store.get(uid)
        self.sessions[uid] = {"mode": "idle"}
        rows, lines = [], ["🏫 **Класс**", ""]
        if u["cls"] and u["cls"] in self.store.classes:
            c = self.store.classes[u["cls"]]
            lines.append(f"Вы в классе «{c['name']}».")
            rows.append([("📋 Рейтинг класса", f"clsv:{u['cls']}")])
            rows.append([("🚪 Выйти из класса", "cls_leave")])
        else:
            lines.append("Вступите в класс по коду учителя, чтобы соревноваться с одноклассниками.")
            rows.append([("🔑 Ввести код класса", "cls_join")])
        mine = [(code, c) for code, c in self.store.classes.items() if str(c["teacher"]) == str(uid)]
        if mine:
            lines += ["", "Ваши классы как учителя:"]
            lines += [f"• {c['name']} — код **{code}**" for code, c in mine]
            rows += [[(f"📊 {c['name']}", f"clsv:{code}")] for code, c in mine]
        lines += ["", "Учитель? Создайте класс: ученики вступят по коду, а вы увидите их результаты и слабые места."]
        rows.append([("👩‍🏫 Создать класс", "cls_new")])
        rows.append(MENU_BTN)
        return [Out("\n".join(lines), rows)]

    def cls_prompt(self, uid, mode):
        self.sessions[uid] = {"mode": mode}
        if mode == "join":
            return [Out("Напишите код класса, который дал учитель (например, K7Q2M).", [MENU_BTN])]
        return [Out("Напишите название класса, например «7Б, школа №3».", [MENU_BTN])]

    def _create_class(self, uid, name):
        name = name.strip()[:40]
        if not name:
            return self.cls_prompt(uid, "newclass")
        code = "".join(random.choice(CODE_ALPHABET) for _ in range(5))
        while code in self.store.classes:
            code = "".join(random.choice(CODE_ALPHABET) for _ in range(5))
        self.store.classes[code] = {"name": name, "teacher": uid, "created": int(time.time())}
        self.store.save()
        txt = (f"✅ Класс «{name}» создан!\n\nКод для учеников: **{code}**\n\n"
               "Скажите его ученикам: в боте нужно нажать «🏫 Класс» → «🔑 Ввести код класса». "
               "Результаты класса появятся здесь, в «🏫 Класс».")
        return [Out(txt, [[(f"📊 {name}", f"clsv:{code}")], MENU_BTN])]

    def _join_class(self, uid, code):
        code = code.strip().upper().replace(" ", "")
        u = self.store.get(uid)
        c = self.store.classes.get(code)
        if not c:
            return [Out("Такого кода нет. Проверьте, нет ли опечатки, и попробуйте ещё раз.",
                        [[("🔑 Ввести код ещё раз", "cls_join")], MENU_BTN])]
        u["cls"] = code
        self.store.save()
        return [Out(f"🎉 Вы в классе «{c['name']}»! Ваши очки теперь идут в рейтинг класса.",
                    [[("📋 Рейтинг класса", f"clsv:{code}")], MENU_BTN])]

    def cls_leave(self, uid):
        u = self.store.get(uid)
        u["cls"] = None
        self.store.save()
        return [Out("Вы вышли из класса.", [[("🏫 Класс", "cls")], MENU_BTN])]

    def class_view(self, uid, code):
        c = self.store.classes.get(code)
        if not c:
            return self.class_menu(uid)
        members = [u for u in self.store.users.values() if u.get("cls") == code]
        members.sort(key=lambda u: -Store.score(u))
        is_teacher = str(c["teacher"]) == str(uid)
        lines = [f"📊 **Класс «{c['name']}»**", f"Код: {code} · учеников: {len(members)}", ""]
        if not members:
            lines.append("Пока никто не вступил. Дайте ученикам код класса.")
        medals = ["🥇", "🥈", "🥉"]
        for i, u in enumerate(members[:30]):
            m = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{m} {u['name']} — {Store.score(u)} очк., пройдено {len(u['best'])}")
        if members:
            hooks, wins, total = {}, 0, 0
            per_sc = {}
            for u in members:
                for h, n in u["hooked_by"].items():
                    hooks[h] = hooks.get(h, 0) + n
                for sid, pl in u["plays"].items():
                    t = pl["w"] + pl["l"] + pl["p"]
                    wins, total = wins + pl["w"], total + t
                    a = per_sc.setdefault(sid, [0, 0])
                    a[0], a[1] = a[0] + pl["w"], a[1] + t
            lines += ["", f"Средний балл: {sum(Store.score(u) for u in members) // len(members)}"]
            if total:
                lines.append(f"Правильных решений: {round(100 * wins / total)}% из {total} попыток")
            if hooks and is_teacher:
                top = sorted(hooks.items(), key=lambda kv: -kv[1])[:3]
                lines += ["", "⚠️ **Чаще всего класс попадается на:**"]
                lines += [f"{self.hooks[h]['emoji']} {self.hooks[h]['title']} — {n} раз" for h, n in top if h in self.hooks]
            hard = [(sid, a[0] / a[1]) for sid, a in per_sc.items() if a[1] >= 3 and sid in self.sc]
            if hard and is_teacher:
                sid, rate = min(hard, key=lambda x: x[1])
                lines.append(f"\nСамая трудная ситуация: {self.sc[sid]['emoji']} {self.sc[sid]['title']} "
                             f"(справились {round(100 * rate)}%)")
        return [Out("\n".join(lines), [[("🏫 Класс", "cls")], MENU_BTN])]

    # ---------- статистика для организаторов (/stats) ----------

    def stats(self):
        users = list(self.store.users.values())
        n = len(users)
        lines = ["📊 **Статистика тренажёра**", "", f"Пользователей: {n}"]
        if not n:
            return [Out("\n".join(lines + ["", "Пока никто не играл."]), [MENU_BTN])]
        auds = {}
        for u in users:
            auds[u.get("aud")] = auds.get(u.get("aud"), 0) + 1
        lines.append(" · ".join(f"{AUDIENCES.get(a, 'не выбрали режим')}: {c}" for a, c in auds.items()))
        w = sum(u.get("wins", 0) for u in users)
        l = sum(u.get("losses", 0) for u in users)
        p = sum(u.get("paranoid", 0) for u in users)
        tot = w + l + p
        lines += ["", f"Пройдено ситуаций: {tot}"]
        if tot:
            lines.append(f"✅ Раскусили обман: {round(100 * w / tot)}% · 🎣 попались: {round(100 * l / tot)}% · "
                         f"🙈 перестраховались: {round(100 * p / tot)}%")
        hooks = {}
        for u in users:
            for h, c in u.get("hooked_by", {}).items():
                hooks[h] = hooks.get(h, 0) + c
        if hooks:
            hsum = sum(hooks.values())
            lines += ["", "🪝 **На какие крючки попадаются чаще всего:**"]
            for h, c in sorted(hooks.items(), key=lambda kv: -kv[1])[:5]:
                if h in self.hooks:
                    bar = "█" * max(1, round(10 * c / hsum))
                    lines.append(f"{self.hooks[h]['emoji']} {self.hooks[h]['title']}: {round(100 * c / hsum)}% {bar}")
        per = {}
        for u in users:
            for sid, pl in u.get("plays", {}).items():
                a = per.setdefault(sid, [0, 0])
                a[0], a[1] = a[0] + pl["w"], a[1] + pl["w"] + pl["l"] + pl["p"]
        if per:
            lines += ["", "🎮 **Ситуации (сколько справились):**"]
            for sid, (ok, t) in sorted(per.items(), key=lambda kv: kv[1][0] / max(kv[1][1], 1)):
                if sid in self.sc and t:
                    lines.append(f"{self.sc[sid]['emoji']} {self.sc[sid]['title']}: {round(100 * ok / t)}% из {t}")
        lines += ["", f"🔍 Проверено сообщений: {sum(u.get('checks', 0) for u in users)}",
                  f"🤖 Боёв с ИИ: {sum(u.get('ai_wins', 0) + u.get('ai_losses', 0) for u in users)}",
                  f"🆘 Открывали «Меня обманули»: {sum(u.get('sos', 0) for u in users)}",
                  f"🏫 Классов: {len(self.store.classes)}"]
        return [Out("\n".join(lines), [MENU_BTN])]

    # ---------- бой с ИИ ----------

    def ai_start(self, uid):
        u = self.store.get(uid)
        if not self.ai:
            return [Out("🤖 Режим с нейросетью не подключён.\n\nОрганизатору: добавьте ключ GigaChat или YandexGPT в config.json (см. README). "
                        "А пока можно потренироваться на готовых ситуациях!", [[("🎮 Тренировка", "play")], MENU_BTN])]
        duel = ai_mod.ScamDuel(self.ai, formal=self.formal(u))
        try:
            turn = duel.opening()
        except ai_mod.AIError as e:
            log_ai_error(e)
            return [Out("😵 Нейросеть сейчас недоступна. Попробуйте позже или потренируйтесь на готовых ситуациях.",
                        [[("🎮 Тренировка", "play")], MENU_BTN])]
        self.sessions[uid] = {"mode": "ai", "duel": duel}
        intro = self.T(u, "🤖 **Бой с ИИ-мошенником**\n\nСейчас тебе напишет нейросеть в роли мошенника. Сюжет каждый раз новый. "
                          "Отвечай своими словами, как в обычной переписке. Раскусишь обман — победа, отдашь код или деньги — поражение.",
                       "🤖 **Бой с ИИ-мошенником**\n\nСейчас вам напишет нейросеть в роли мошенника. Сюжет каждый раз новый. "
                       "Отвечайте своими словами, как в обычной переписке. Распознаете обман — победа, отдадите код или деньги — поражение.")
        return [Out(intro), Out(f"💬 **Незнакомец:**\n{turn.get('reply', '…')}", [[("⏹ Сдаться и выйти", "ai_stop")]], typing=True)]

    def ai_turn(self, uid, name, text):
        s = self.sessions[uid]
        duel = s["duel"]
        try:
            turn = duel.reply(text)
        except ai_mod.AIError as e:
            log_ai_error(e)
            return [Out("😵 Нейросеть не ответила. Напишите ещё раз или выйдите в меню.", [[("⏹ Выйти", "ai_stop")]])]
        u = self.store.get(uid, name)
        st = turn["status"]
        if st == "continue" and duel.turns >= 8:
            st, turn["explain"] = "safe", "Вы продержались 8 ходов и ничего не отдали. Мошенник сдался!"
        if st == "continue":
            return [Out(f"💬 **Незнакомец:**\n{turn.get('reply', '…')}", [[("⏹ Сдаться и выйти", "ai_stop")]], typing=True)]
        self.sessions[uid] = {"mode": "idle"}
        rows = [[("🤖 Ещё бой", "ai")], MENU_BTN]
        if st == "safe":
            u["ai_wins"] += 1
            self.store.save()
            return [Out(f"✅ **Победа! ИИ-мошенник раскрыт.**\n\n{turn.get('explain', '')}\n\n🏅 +15 очков", rows)]
        u["ai_losses"] += 1
        hid = turn.get("hook")
        if hid in self.hooks:
            u["hooked_by"][hid] = u["hooked_by"].get(hid, 0) + 1
        self.store.save()
        hk = self.hooks.get(hid)
        head = f"🎣 **Попались! Крючок: {hk['emoji']} {hk['title']}**" if hk else "🎣 **Попались!**"
        defense = f"\n\n🛡️ **Как защититься:** {hk['defense_v' if self.formal(u) else 'defense']}" if hk else ""
        return [Out(f"{head}\n\n{turn.get('explain', '')}{defense}", [[("🆘 Что делать в жизни", "sos")]] + rows)]
