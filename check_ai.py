"""Проверка подключения нейросети. Запуск: python check_ai.py (или check_ai.bat).

Пошагово проверяет связь с GigaChat / YandexGPT и пишет, что именно не так.
"""
import sys

from main import load_config
from max_api import make_ssl_context
import ai

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    cfg = load_config()
    prov = (cfg.get("ai_provider") or "").lower()
    print("Провайдер в config.json:", prov or "(не указан)")
    if prov == "gigachat":
        key = (cfg.get("gigachat_auth_key") or "").strip()
        print("Ключ авторизации:", f"есть, {len(key)} символов" if key else "ПУСТО — впишите gigachat_auth_key")
        if key and (" " in key.replace("Basic ", "", 1) or len(key) < 40):
            print("  ⚠️ Ключ выглядит странно: в нём пробелы или он слишком короткий. Нужен «Ключ авторизации» (длинная строка).")
    ctx = make_ssl_context()
    provider = ai.make_provider(cfg, ctx)
    if not provider:
        print("\n❌ Нейросеть не настроена. Нужно: \"ai_provider\": \"gigachat\" и \"gigachat_auth_key\": \"...\"")
        return

    if isinstance(provider, ai.GigaChat):
        print("\nШаг 1. Получаю токен GigaChat (ngw.devices.sberbank.ru:9443)...")
        try:
            provider._get_token()
            print("   ✅ Токен получен")
        except ai.AIError as e:
            print("   ❌", e)
            return
        except Exception as e:
            print("   ❌ Неожиданная ошибка:", repr(e))
            return

    print("\nШаг 2. Отправляю нейросети «Привет»...")
    try:
        answer = provider.chat([{"role": "user", "text": "Привет! Ответь одним коротким предложением."}])
        print("   ✅ Нейросеть ответила:", answer.strip()[:200])
    except ai.AIError as e:
        print("   ❌", e)
        return

    print("\nШаг 3. Пробую начать «Бой с ИИ-мошенником»...")
    try:
        turn = ai.ScamDuel(provider).opening()
        print("   ✅ Первая реплика мошенника:", turn.get("reply", turn))
    except ai.AIError as e:
        print("   ❌", e)
        return
    print("\n🎉 Всё работает! Запускайте бота через start.bat.")


if __name__ == "__main__":
    main()
