"""Пересобирает web/demo.html из scenarios.json и hooks.json (запускать после правки сценариев)."""
import json, os
d = os.path.dirname(os.path.abspath(__file__))
t = open(os.path.join(d, "template.html"), encoding="utf-8").read()
sc = json.dumps(json.load(open(os.path.join(d, "..", "scenarios.json"), encoding="utf-8")), ensure_ascii=False)
hk = json.dumps(json.load(open(os.path.join(d, "..", "hooks.json"), encoding="utf-8")), ensure_ascii=False)
open(os.path.join(d, "demo.html"), "w", encoding="utf-8").write(t.replace("__SCENARIOS__", sc).replace("__HOOKS__", hk))
print("demo.html обновлён")
