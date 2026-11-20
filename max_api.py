"""Минимальный клиент MAX Bot API на стандартной библиотеке Python (ничего не надо устанавливать).

Документация: https://dev.max.ru/docs-api
- адрес API: https://platform-api2.max.ru
- токен передаётся в заголовке Authorization (в адресе запроса он больше не поддерживается)
- сертификат сервера выдан Минцифры, поэтому нужен корневой сертификат (см. README)
"""
import glob
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://platform-api2.max.ru"
BASE = os.path.dirname(os.path.abspath(__file__))


def make_ssl_context(extra_ca=None):
    """Системные сертификаты + сертификаты Минцифры из папки certs/ (форматы .cer/.crt/.pem, DER или PEM)."""
    ctx = ssl.create_default_context()
    files = list(extra_ca or [])
    files += glob.glob(os.path.join(BASE, "certs", "*.cer"))
    files += glob.glob(os.path.join(BASE, "certs", "*.crt"))
    files += glob.glob(os.path.join(BASE, "certs", "*.pem"))
    for path in files:
        try:
            ctx.load_verify_locations(cafile=path)
        except ssl.SSLError:
            with open(path, "rb") as f:
                ctx.load_verify_locations(cadata=f.read())  # DER-формат
    return ctx


class MaxError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


class MaxAPI:
    def __init__(self, token, base=API, ctx=None):
        self.token, self.base = token.strip(), base.rstrip("/")
        self.ctx = ctx or make_ssl_context()
        self._last_send = {}

    def _req(self, method, path, params=None, body=None, timeout=40):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": self.token, "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
                    raw = r.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                txt = e.read().decode("utf-8", "replace")
                if e.code == 429 and attempt < 2:
                    time.sleep(1.5)
                    continue
                raise MaxError(e.code, txt) from None

    # --- методы API ---

    def get_me(self):
        return self._req("GET", "/me")

    def get_updates(self, marker=None, timeout=30):
        return self._req("GET", "/updates",
                         {"marker": marker, "timeout": timeout, "limit": 100,
                          "types": "message_created,message_callback,bot_started"},
                         timeout=timeout + 15)

    @staticmethod
    def keyboard(buttons):
        if not buttons:
            return None
        return [{"type": "inline_keyboard", "payload": {"buttons": [
            [{"type": "callback", "text": t[:128], "payload": p} for t, p in row] for row in buttons]}}]

    def send(self, text, buttons=None, chat_id=None, user_id=None):
        # Не больше 2 сообщений в секунду в один чат — делаем паузу
        key = chat_id or user_id
        wait = 0.6 - (time.time() - self._last_send.get(key, 0))
        if wait > 0:
            time.sleep(wait)
        body = {"text": text[:4000], "format": "markdown"}
        kb = self.keyboard(buttons)
        if kb:
            body["attachments"] = kb
        params = {"chat_id": chat_id} if chat_id else {"user_id": user_id}
        res = self._req("POST", "/messages", params, body)
        self._last_send[key] = time.time()
        return res

    def answer_callback(self, callback_id, new_text=None, notification=None):
        """Отвечает на нажатие кнопки. new_text заменяет текст нажатого сообщения и убирает кнопки."""
        body = {}
        if new_text is not None:
            body["message"] = {"text": new_text[:4000], "attachments": []}
        if notification:
            body["notification"] = notification
        return self._req("POST", "/answers", {"callback_id": callback_id}, body)
