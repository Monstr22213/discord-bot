import os
from flask import Flask, request, redirect, session, render_template_string, jsonify
import config

app = Flask(__name__)
app.secret_key = os.getenv("PANEL_SECRET", os.getenv("DISCORD_TOKEN", "secret")[:16] + "panel")

PANEL_USER = os.getenv("PANEL_USER", "admin")
PANEL_PASS = os.getenv("PANEL_PASS", "admin123")

HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Uzi Bot Panel</title>
<style>
:root{--bg:#0a0a12;--card:#16162a;--b:#232342;--acc:#7c3aed;--acc2:#a78bfa;--ok:#10b981}
*{box-sizing:border-box}
body{font-family:'Inter',system-ui,sans-serif; background:radial-gradient(1200px 600px at 20% -10%, #2a1a5e 0%, transparent 60%), var(--bg); color:#e2e8f0; min-height:100vh; margin:0; padding:0}
.wrap{max-width:900px; margin:0 auto; padding:28px 20px}
h1{font-size:28px; margin:0 0 8px; display:flex; align-items:center; gap:10px}
h1 span{font-size:12px; background:var(--acc); padding:4px 8px; border-radius:20px; color:white}
.sub{opacity:.6; font-size:13px; margin-bottom:18px}
.card{background:var(--card); border:1px solid var(--b); padding:16px; border-radius:16px; margin:14px 0; box-shadow:0 8px 32px rgba(0,0,0,.3)}
.grid{display:grid; grid-template-columns:1fr 1fr; gap:12px}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
label{font-size:11px; letter-spacing:.08em; text-transform:uppercase; opacity:.7; margin:14px 0 6px; display:block; font-weight:700}
input,textarea,select{width:100%; padding:12px 12px; border-radius:10px; border:1px solid var(--b); background:#0f0f1e; color:#e2e8f0; outline:none; transition:.2s}
input:focus,textarea:focus{border-color:var(--acc); box-shadow:0 0 0 3px rgba(124,58,237,.2)}
textarea{min-height:340px; font-family:ui-monospace, monospace; font-size:13px; line-height:1.5; resize:vertical}
.btn{appearance:none; background:var(--acc); color:white; padding:11px 18px; border:none; border-radius:10px; cursor:pointer; font-weight:700; display:inline-flex; gap:8px; align-items:center}
.btn:hover{background:#6d28d9}
.btn-ghost{background:transparent; border:1px solid var(--b); color:#cbd5e1}
.btn-ghost:hover{background:var(--card)}
.alert{padding:12px 14px; border-radius:10px; margin:12px 0; font-size:14px}
.ok{background:rgba(16,185,129,.15); border:1px solid rgba(16,185,129,.3); color:#6ee7b7}
.err{background:rgba(239,68,68,.12); border:1px solid rgba(239,68,68,.3); color:#fca5a5}
.status{display:flex; flex-wrap:wrap; gap:8px; align-items:center}
.badge{background:#0f0f1e; border:1px solid var(--b); padding:6px 10px; border-radius:20px; font-size:12px}
.badge b{color:var(--acc2)}
a{color:var(--acc2); text-decoration:none}
.top{display:flex; justify-content:space-between; align-items:center; gap:10px}
.env-row{display:flex; gap:8px; align-items:center; margin:6px 0; font-size:13px}
.env-k{min-width:160px; font-family:monospace; color:var(--acc2)}
.env-v{flex:1; background:#0f0f1e; border:1px solid var(--b); padding:8px 10px; border-radius:8px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
</style>
</head>
<body>
<div class="wrap">
<h1>🤖 Uzi Bot Panel <span>Opencode Zen</span></h1>
<div class="sub">Сайт для настройки бота — промпт, модель и триггеры меняются на лету. Для постоянства дублируй в Railway Variables.</div>
{% if msg %}<div class="alert {{ 'ok' if ok else 'err' }}">{{msg}}</div>{% endif %}
{% if not logged %}
<div class="card">
<form method="post" action="/login">
<label>Логин</label><input name="user" value="admin">
<label>Пароль</label><input name="pass" type="password" placeholder="admin123">
<button class="btn" style="margin-top:14px">Войти →</button>
</form>
<p style="opacity:.5; font-size:12px; margin-top:10px">По умолчанию <code>admin / admin123</code> — поменяй в Railway Variables <code>PANEL_USER</code> / <code>PANEL_PASS</code></p>
</div>
{% else %}
<div class="card">
<div class="top">
<div class="status">
<span class="badge"><b>Model:</b> {{model}}</span>
<span class="badge"><b>Base:</b> {{base}}</span>
<span class="badge"><b>Триггеры:</b> {{triggers or '—'}}</span>
</div>
<a href="/logout" class="btn-ghost" style="padding:6px 12px; border-radius:20px; font-size:13px">Выйти</a>
</div>
</div>

<div class="card" style="border-style:dashed">
<div style="font-weight:800; margin-bottom:8px">🧬 Текущие ENV (что видит бот прямо сейчас)</div>
<div class="env-row"><span class="env-k">AI_MODEL</span><span class="env-v">{{env_model}}</span></div>
<div class="env-row"><span class="env-k">AI_BASE_URL</span><span class="env-v">{{env_base}}</span></div>
<div class="env-row"><span class="env-k">AI_TRIGGER_NAMES</span><span class="env-v">{{env_triggers}}</span></div>
<div class="env-row"><span class="env-k">AI_SYSTEM_PROMPT</span><span class="env-v" style="white-space:pre-wrap; max-height:120px; overflow:auto">{{env_prompt[:300]}} {% if env_prompt|length > 300 %}… ({{env_prompt|length}} симв.){% endif %}</span></div>
<div style="font-size:12px; opacity:.6; margin-top:8px">↑ Это реальные <code>os.getenv</code> значения. Если пусто — берется дефолт из <code>config.py</code>. На Railway для постоянства меняй в <b>Variables</b>, а не только тут.</div>
</div>

<form method="post" action="/save">
<div class="grid">
<div>
<label>AI_MODEL</label>
<input name="model" value="{{model}}" placeholder="muse-spark-1.2-contributor-free">
<div style="font-size:11px; opacity:.5; margin-top:4px">Напр. <code>muse-spark-1.2-contributor-free</code> (Opencode) или <code>openai/gpt-oss-20b:free</code> (OpenRouter)</div>
</div>
<div>
<label>AI_BASE_URL</label>
<input name="base" value="{{base}}" placeholder="https://opencode.ai/zen/v1/responses">
</div>
</div>
<label>AI_TRIGGER_NAMES <span style="text-transform:none; opacity:.5; font-weight:400">— через запятую, на что реагирует: узи, uzi, анечка, бот</span></label>
<input name="triggers" value="{{triggers}}" placeholder="бот,анечка,узи,uzi">
<label>AI_SYSTEM_PROMPT <span style="text-transform:none; opacity:.6; font-weight:400">— длинный промпт Узи (поддержка многострочного, сохраняется в памяти + .env, на Railway — до перезапуска, для постоянства → Variables)</span></label>
<textarea name="prompt" placeholder="Ты — Узи...">{{prompt}}</textarea>
<div style="display:flex; gap:10px; flex-wrap:wrap">
<button class="btn" type="submit">💾 Сохранить и применить</button>
<a class="btn btn-ghost" href="/?test=1">🧪 Тест: Узи как дела?</a>
<a class="btn btn-ghost" href="/clear" onclick="return confirm('Очистить историю AI?')">🧹 Очистить историю</a>
</div>
</form>

<div class="card" style="background:rgba(124,58,237,.08)">
<h3 style="margin:0 0 8px">Функции</h3>
<ul style="margin:0; padding-left:18px; line-height:1.8; font-size:13px">
<li><b>На лету</b> — без перезапуска, бот сразу отвечает по новому промпту.</li>
<li><b>ENV</b> — блок выше показывает что реально в <code>os.environ</code>. Пусто = дефолт из <code>config.py</code>.</li>
<li><b>Постоянство на Railway:</b> `Variables` → `AI_SYSTEM_PROMPT` / `AI_MODEL` → Save (перезапустит бота).</li>
<li><b>Триггеры:</b> добавь ник друга в `AI_TRIGGER_NAMES` чтобы бот реагировал на него.</li>
</ul>
</div>
{% endif %}
</div>
</body>
</html>
"""

def get_cfg():
    # важно: or fallback если env пустой
    return {
        "model": os.getenv("AI_MODEL") or config.AI_MODEL,
        "base": os.getenv("AI_BASE_URL") or config.AI_BASE_URL,
        "triggers": os.getenv("AI_TRIGGER_NAMES") if os.getenv("AI_TRIGGER_NAMES") is not None else getattr(config, "AI_TRIGGER_NAMES_RAW", ""),
        "prompt": os.getenv("AI_SYSTEM_PROMPT") or config.AI_SYSTEM_PROMPT,
        "env_model": os.getenv("AI_MODEL") or "(дефолт) "+config.AI_MODEL,
        "env_base": os.getenv("AI_BASE_URL") or "(дефолт) "+config.AI_BASE_URL,
        "env_triggers": os.getenv("AI_TRIGGER_NAMES") if os.getenv("AI_TRIGGER_NAMES") is not None else "(дефолт) "+getattr(config, "AI_TRIGGER_NAMES_RAW", ""),
        "env_prompt": os.getenv("AI_SYSTEM_PROMPT") or config.AI_SYSTEM_PROMPT,
    }

@app.route("/", methods=["GET"])
def index():
    logged = session.get("logged")
    cfg = get_cfg()
    return render_template_string(HTML, logged=logged, msg=request.args.get("msg"), ok=request.args.get("ok")=="1", **cfg)

@app.route("/login", methods=["POST"])
def login():
    if request.form.get("user")==PANEL_USER and request.form.get("pass")==PANEL_PASS:
        session["logged"]=True
        return redirect("/")
    return redirect("/?msg=Неверный логин/пароль&ok=0")

@app.route("/logout")
def logout():
    session.pop("logged",None)
    return redirect("/")

@app.route("/clear")
def clear():
    if not session.get("logged"):
        return redirect("/")
    # чистим историю AI (импорт ленивый чтобы не циклить)
    try:
        import bot as _bot
        for k in list(_bot._ai_history.keys()):
            _bot._ai_history[k].clear()
        msg="История AI очищена"
    except Exception as e:
        msg=f"Ошибка очистки: {e}"
    return redirect(f"/?msg={msg}&ok=1")

@app.route("/save", methods=["POST"])
def save():
    if not session.get("logged"):
        return redirect("/")
    model = request.form.get("model","").strip()
    base = request.form.get("base","").strip()
    triggers = request.form.get("triggers","").strip()
    prompt = request.form.get("prompt","").strip()
    # ставим в env для текущего процесса — бот читает через os.getenv/_cfg на лету
    if model: os.environ["AI_MODEL"]=model
    else: os.environ.pop("AI_MODEL",None)
    if base: os.environ["AI_BASE_URL"]=base
    else: os.environ.pop("AI_BASE_URL",None)
    # триггеры можно пусто — тогда дефолт
    os.environ["AI_TRIGGER_NAMES"]=triggers
    if prompt: os.environ["AI_SYSTEM_PROMPT"]=prompt
    else: os.environ.pop("AI_SYSTEM_PROMPT",None)
    # пытаемся сохранить в .env файл для локалки
    try:
        env_path = ".env"
        if os.path.exists(env_path):
            with open(env_path,"r",encoding="utf-8") as f: content=f.read()
            def upsert(key,val):
                nonlocal content
                import re
                esc=re.escape(key)
                if re.search(rf"^{esc}=.*", content, flags=re.M):
                    # для промпта с переносами — пишем в одну строку с \n
                    safe=val.replace("\n","\\n") if key=="AI_SYSTEM_PROMPT" else val
                    content=re.sub(rf"^{esc}=.*", f"{key}={safe}", content, flags=re.M)
                else:
                    safe=val.replace("\n","\\n") if key=="AI_SYSTEM_PROMPT" else val
                    content+=f"\n{key}={safe}"
                return content
            upsert("AI_MODEL", model or config.AI_MODEL)
            upsert("AI_BASE_URL", base or config.AI_BASE_URL)
            upsert("AI_TRIGGER_NAMES", triggers)
            if prompt:
                upsert("AI_SYSTEM_PROMPT", prompt)
            with open(env_path,"w",encoding="utf-8") as f: f.write(content)
    except Exception as e:
        print("panel save .env fail", e)
    return redirect("/?msg=Сохранено! Бот уже использует новый промпт &ok=1")

if __name__=="__main__":
    port=int(os.getenv("PORT","8080"))
    app.run(host="0.0.0.0", port=port)
