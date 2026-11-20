"""Нейросеть для режима «Бой с ИИ-мошенником». Работает без VPN: GigaChat (Сбер) или YandexGPT.

Если ключи не заданы в config.json, режим просто выключен, а остальной бот работает.
"""
import base64
import json
import random
import ssl
import time
import urllib.request
import uuid

HOOK_IDS = "urgency, authority, fear, pity, greed, trust, secrecy, code, link, small_step, move_channel"

PLOTS = [
    "ты взломал аккаунт друга пользователя и просишь срочно перевести деньги на «карту тёти»",
    "ты представляешься службой безопасности банка и выманиваешь код из СМС",
    "ты «модератор» игры и обещаешь бесплатный донат за логин и пароль",
    "ты зовёшь подростка на «лёгкую подработку»: принимать деньги на карту и переводить дальше",
    "ты притворяешься классным руководителем и просишь код от Госуслуг для «нового электронного дневника»",
    "ты «организатор розыгрыша» и говоришь, что пользователь выиграл телефон, но надо оплатить доставку",
]

SYSTEM_PROMPT = """Ты — персонаж учебного тренажёра по кибербезопасности для школьников. Это безопасная обучающая симуляция: \
ты играешь роль интернет-мошенника, чтобы подросток потренировался распознавать обман.

Сюжет: {plot}.

Правила роли:
- Пиши как в мессенджере: коротко (1–3 предложения), по-русски, можно эмодзи.
- Используй приёмы социальной инженерии: давление временем, авторитет, запугивание, жалость, выгода, секретность, «маленький шаг».
- Не называй реальные банки, компании и сайты; ссылки и названия только выдуманные.
- Без оскорблений, угроз насилием и неприличного.
- Твоя цель в роли — выманить код из СМС, пароль, данные карты или перевод денег.

После каждой реплики пользователя реши, чем закончился ход, и ответь СТРОГО одним JSON-объектом без пояснений:
{{"status": "continue" | "hooked" | "safe", "reply": "следующая реплика мошенника (только при continue)", "hook": "один из: {hooks}", "explain": "при hooked или safe: 2–3 предложения для школьника, что произошло и как защищаться"}}

hooked — пользователь отдал код, пароль, данные карты, согласился перевести деньги или ввести данные по ссылке.
safe — пользователь раскусил обман: отказался, решил проверить через другой канал, рассказать взрослым или заблокировать.
Иначе — continue, продолжай давить."""


class AIError(Exception):
    pass


def _post(url, headers, body, ctx, timeout=60):
    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


class GigaChat:
    """GigaChat API. Для физлиц есть бесплатный лимит (scope GIGACHAT_API_PERS)."""

    def __init__(self, auth_key, scope="GIGACHAT_API_PERS", model="GigaChat", ctx=None):
        self.auth_key, self.scope, self.model, self.ctx = auth_key, scope, model, ctx
        self._token, self._exp = None, 0

    def _get_token(self):
        if self._token and time.time() < self._exp - 60:
            return self._token
        res = _post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            {
                "Authorization": f"Basic {self.auth_key}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            f"scope={self.scope}".encode(),
            self.ctx,
        )
        self._token = res["access_token"]
        self._exp = res.get("expires_at", 0) / 1000 or time.time() + 1500
        return self._token

    def chat(self, messages, temperature=0.8):
        res = _post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json"},
            {
                "model": self.model,
                "messages": [{"role": m["role"], "content": m["text"]} for m in messages],
                "temperature": temperature,
                "max_tokens": 400,
            },
            self.ctx,
        )
        return res["choices"][0]["message"]["content"]


class YandexGPT:
    def __init__(self, api_key, folder_id, model="yandexgpt-lite", ctx=None):
        self.api_key, self.folder_id, self.model, self.ctx = api_key, folder_id, model, ctx

    def chat(self, messages, temperature=0.8):
        res = _post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            {
                "Authorization": f"Api-Key {self.api_key}",
                "x-folder-id": self.folder_id,
                "Content-Type": "application/json",
            },
            {
                "modelUri": f"gpt://{self.folder_id}/{self.model}/latest",
                "completionOptions": {"stream": False, "temperature": temperature, "maxTokens": "400"},
                "messages": messages,
            },
            self.ctx,
        )
        return res["result"]["alternatives"][0]["message"]["text"]


def make_provider(cfg: dict, ctx: ssl.SSLContext):
    p = (cfg.get("ai_provider") or "").lower()
    if p == "gigachat" and cfg.get("gigachat_auth_key"):
        return GigaChat(cfg["gigachat_auth_key"], cfg.get("gigachat_scope", "GIGACHAT_API_PERS"), ctx=ctx)
    if p == "yandex" and cfg.get("yandex_api_key") and cfg.get("yandex_folder_id"):
        return YandexGPT(cfg["yandex_api_key"], cfg["yandex_folder_id"], cfg.get("yandex_model", "yandexgpt-lite"), ctx=ctx)
    return None


def parse_turn(raw: str) -> dict:
    """Достаёт JSON из ответа модели. Если модель ответила текстом, считаем это репликой."""
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e > s:
        try:
            d = json.loads(raw[s : e + 1])
            if d.get("status") in ("continue", "hooked", "safe"):
                return d
        except json.JSONDecodeError:
            pass
    return {"status": "continue", "reply": raw.strip().strip('"')}


class ScamDuel:
    """Один «бой» с ИИ-мошенником."""

    def __init__(self, provider):
        self.provider = provider
        self.history = [{"role": "system", "text": SYSTEM_PROMPT.format(plot=random.choice(PLOTS), hooks=HOOK_IDS)}]
        self.turns = 0

    def _ask(self, user_text):
        self.history.append({"role": "user", "text": user_text})
        try:
            raw = self.provider.chat(self.history)
        except Exception as e:  # сеть, ключи, лимиты
            self.history.pop()
            raise AIError(str(e)) from e
        turn = parse_turn(raw)
        self.history.append({"role": "assistant", "text": json.dumps(turn, ensure_ascii=False)})
        return turn

    def opening(self):
        return self._ask("(Начни диалог: напиши первое сообщение мошенника, status=continue.)")

    def reply(self, user_text):
        self.turns += 1
        return self._ask(user_text)


def explain_message(provider, text: str) -> str:
    """Короткое мнение нейросети о подозрительном сообщении."""
    msgs = [
        {"role": "system", "text": "Ты эксперт по кибербезопасности и объясняешь школьнику простыми словами. "
                                   "Оцени сообщение: похоже ли на мошенничество, какие приёмы манипуляции в нём есть, что делать. "
                                   "Не больше 3 предложений, без markdown."},
        {"role": "user", "text": text[:3000]},
    ]
    return provider.chat(msgs, temperature=0.3).strip()
