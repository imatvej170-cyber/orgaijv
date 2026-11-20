"""Бот-тренажёр «Фейковый друг» для мессенджера MAX.

Запуск:
    python main.py            — бот в MAX (нужен токен в config.json)
    python main.py --console  — проверка без MAX, прямо в терминале
"""
import json
import os
import sys
import threading
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from engine import Engine
import ai as ai_mod
from max_api import MaxAPI, MaxError, make_ssl_context

BASE = os.path.dirname(os.path.abspath(__file__))

if hasattr(sys.stdout, "reconfigure"):  # эмодзи в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_config():
    cfg = {}
    path = os.path.join(BASE, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    for key in ("max_token", "ai_provider", "gigachat_auth_key", "gigachat_scope",
                "yandex_api_key", "yandex_folder_id", "yandex_model"):
        if os.environ.get(key.upper()):
            cfg[key] = os.environ[key.upper()]
    return cfg


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ====================== MAX ======================

class MaxBot:
    def __init__(self, api: MaxAPI, engine: Engine):
        self.api, self.engine = api, engine
        self.pool = ThreadPoolExecutor(max_workers=32)
        self.user_locks = {}
        self.sent = OrderedDict()  # mid -> исходный текст с разметкой (для правки после нажатия кнопки)

    def _lock(self, uid):
        return self.user_locks.setdefault(uid, threading.Lock())

    def deliver(self, outs, chat_id=None, user_id=None):
        for o in outs:
            try:
                if o.typing and chat_id:  # «мошенник печатает…» — как в настоящей переписке
                    self.api.send_action(chat_id)
                    time.sleep(min(2.5, 0.8 + len(o.text) / 90))
                res = self.api.send(o.text, o.buttons, chat_id=chat_id, user_id=user_id)
                mid = (res.get("message") or {}).get("body", {}).get("mid")
                if mid:
                    self.sent[mid] = o.text
                    while len(self.sent) > 5000:
                        self.sent.popitem(last=False)
            except MaxError as e:
                log("Ошибка отправки:", e)

    def handle(self, upd):
        t = upd.get("update_type")
        if t == "bot_started":
            user = upd.get("user") or {}
            uid = user.get("user_id")
            with self._lock(uid):
                outs = self.engine.on_start(uid, user.get("first_name") or user.get("name"), upd.get("payload"))
                self.deliver(outs, chat_id=upd.get("chat_id"), user_id=uid)

        elif t == "message_created":
            msg = upd.get("message") or {}
            sender = msg.get("sender") or {}
            if sender.get("is_bot"):
                return
            uid = sender.get("user_id")
            chat_id = (msg.get("recipient") or {}).get("chat_id")
            text = (msg.get("body") or {}).get("text") or ""
            link = msg.get("link") or {}
            if link.get("type") == "forward":  # пересланное сообщение — берём его текст
                fwd = ((link.get("message") or {}).get("text")) or ""
                text = (text + "\n" + fwd).strip()
            with self._lock(uid):
                outs = self.engine.on_text(uid, sender.get("first_name") or sender.get("name"), text)
                self.deliver(outs, chat_id=chat_id, user_id=uid)

        elif t == "message_callback":
            cb = upd.get("callback") or {}
            user = cb.get("user") or {}
            uid = user.get("user_id")
            msg = upd.get("message") or cb.get("message") or {}
            chat_id = (msg.get("recipient") or {}).get("chat_id")
            with self._lock(uid):
                label, outs = self.engine.on_callback(uid, user.get("first_name") or user.get("name"), cb.get("payload", ""))
                self._ack(cb.get("callback_id"), msg, label)
                self.deliver(outs, chat_id=chat_id, user_id=uid)

    def _ack(self, callback_id, msg, label):
        """Убираем кнопки с нажатого сообщения и дописываем выбранный ответ."""
        if not callback_id:
            return
        body = msg.get("body") or {}
        orig = self.sent.get(body.get("mid")) or body.get("text") or ""
        new_text = f"{orig}\n\n**{label}**" if label else orig
        try:
            if new_text:
                self.api._req("POST", "/answers", {"callback_id": callback_id},
                              {"message": {"text": new_text[:4000], "format": "markdown", "attachments": []}})
            else:
                self.api.answer_callback(callback_id, notification=label or "✓")
        except MaxError as e:
            log("Не удалось ответить на нажатие:", e)
            try:
                self.api.answer_callback(callback_id, notification=label or "✓")
            except MaxError:
                pass

    def _safe_handle(self, upd):
        try:
            self.handle(upd)
        except Exception:
            log("Ошибка обработки события:\n" + traceback.format_exc())

    def run(self):
        try:
            me = self.api.get_me()
        except MaxError as e:
            if e.status == 401:
                log("❌ Токен не подходит (401). Проверьте max_token в config.json.")
                return
            raise
        except Exception as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                certs = os.path.join(BASE, "certs")
                found = [f for f in os.listdir(certs) if f.lower().endswith((".cer", ".crt", ".pem"))] if os.path.isdir(certs) else []
                log("❌ Нет доверия к сертификату MAX (Минцифры).")
                log("   Файлы сертификатов в папке certs: " + (", ".join(found) if found else "НЕТ НИ ОДНОГО"))
                log("   Скачайте корневой сертификат «Russian Trusted Root CA» с https://www.gosuslugi.ru/crt")
                log("   и положите файл .cer/.crt в папку: " + certs)
            else:
                log("❌ Нет связи с MAX:", e)
            return
        if me.get("username"):
            self.engine.bot_username = me["username"]
        log(f"Бот запущен: {me.get('first_name') or me.get('name')} ({me.get('username')}). Остановить: Ctrl+C")
        log("Статистика для презентации: напишите боту /stats")
        marker = None
        while True:
            try:
                data = self.api.get_updates(marker)
                for upd in data.get("updates", []):
                    self.pool.submit(self._safe_handle, upd)
                marker = data.get("marker", marker)
            except MaxError as e:
                if e.status == 401:
                    log("Токен не подходит (401). Проверьте max_token в config.json.")
                    return
                log("Ошибка API:", e)
                time.sleep(3)
            except Exception as e:  # сеть
                log("Нет связи с MAX:", e)
                time.sleep(3)


# ====================== Консоль ======================

def run_console(engine: Engine):
    uid, name = 1, "Тестер"
    print("Консольный режим. Цифра — нажать кнопку, текст — написать сообщение, q — выход.\n")
    buttons = []

    def show(outs):
        nonlocal buttons
        for o in outs:
            print(o.text.replace("**", ""))
            if o.buttons:
                buttons = [b for row in o.buttons for b in row]
                for i, (t, _) in enumerate(buttons, 1):
                    print(f"   [{i}] {t}")
            print()

    show(engine.on_start(uid, name))
    while True:
        try:
            s = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if s == "q":
            break
        if s.isdigit() and 1 <= int(s) <= len(buttons):
            label, outs = engine.on_callback(uid, name, buttons[int(s) - 1][1])
            if label:
                print(f"   ({label})\n")
            show(outs)
        else:
            show(engine.on_text(uid, name, s))


def main():
    cfg = load_config()
    ctx = make_ssl_context()
    provider = ai_mod.make_provider(cfg, ctx)
    # На сервере Amvera прогресс хранится в постоянной папке /data, чтобы не пропадать при обновлении бота
    data_dir = os.environ.get("DATA_DIR") or ("/data" if os.path.isdir("/data") else os.path.join(BASE, "data"))
    log("Прогресс игроков хранится в:", data_dir)
    engine = Engine(ai_provider=provider, data_path=os.path.join(data_dir, "users.json"))
    log("Нейросеть:", cfg.get("ai_provider") if provider else "не подключена (режим «Бой с ИИ» выключен)")
    if "--console" in sys.argv:
        run_console(engine)
        return
    token = cfg.get("max_token", "")
    if not token or token.startswith("ВСТАВЬТЕ"):
        print("Не задан токен MAX. Впишите его в config.json (см. README) или запустите: python main.py --console")
        sys.exit(1)
    MaxBot(MaxAPI(token, ctx=ctx), engine).run()


if __name__ == "__main__":
    main()
