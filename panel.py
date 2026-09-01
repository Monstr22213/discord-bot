import os
from flask import Flask, request, redirect, session, render_template_string
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
body{font-family:Inter, sans-serif; background:#0f0f14; color:#eee; max-width:800px; margin:30px auto; padding:20px}
h1{color:#c084fc}
label{display:block; margin:12px 0 4px; font-weight:600}
input,textarea,select{width:100%; padding:10px; border-radius:8px; border:1px solid #333; background:#1a1a22; color:#eee}
textarea{min-height:220px; font-family:monospace}
button{background:#7c3aed; color:white; padding:10px 18px; border:none; border-radius:8px; cursor:pointer; margin-top:14px}
button:hover{background:#6d28d9}
.card{background:#1a1a22; padding:16px; border-radius:12px; margin:12px 0}
.alert{padding:10px; border-radius:8px; margin:10px 0}
.ok{background:#14532d; color:#86efac}
.err{background:#7f1d1d; color:#fecaca}
a{color:#a78bfa}
</style>
</head>
<body>
<h1>🤖 Uzi Bot Panel</h1>
{% if msg %}<div class="alert {{ 'ok' if ok else 'err' }}">{{msg}}</div>{% endif %}
{% if not logged %}
<form method="post" action="/login">
<label>Логин</label><input name="user" value="admin">
<label>Пароль</label><input name="pass" type="password">
<button>Войти</button>
<p style="opacity:.6">По умолчанию admin / admin123 — поменяй в Variables PANEL_USER / PANEL_PASS</p>
</form>
{% else %}
<div class="card">
<b>Статус:</b> Модель: <code>{{model}}</code> | Base: <code>{{base}}</code> | Триггеры: <code>{{triggers}}</code>
<a href="/logout" style="float:right">Выйти</a>
</div>
<form method="post" action="/save">
<label>AI_MODEL (напр. muse-spark-1.2-contributor-free или openai/gpt-oss-20b:free)</label>
<input name="model" value="{{model}}">
<label>AI_BASE_URL</label>
<input name="base" value="{{base}}">
<label>AI_TRIGGER_NAMES (через запятую, на что реагирует: узи, uzi, анечка...)</label>
<input name="triggers" value="{{triggers}}">
<label>AI_SYSTEM_PROMPT — длинный промпт Узи (сохранится в памяти и .env, на Railway — в памяти до перезапуска, для постоянства меняй Variables)</label>
<textarea name="prompt">{{prompt}}</textarea>
<button>💾 Сохранить</button>
</form>
<div class="card">
<h3>Подсказки</h3>
<p>Сайт меняет настройки <b>на лету без перезапуска</b> — бот сразу отвечает по новому промпту. Для постоянства на Railway продублируй изменения в <b>Variables -> AI_SYSTEM_PROMPT / AI_MODEL</b>.</p>
<p>Панель: <code>https://твой-домен.railway.app</code> (порт 8080). Локально: <code>http://localhost:8080</code></p>
</div>
{% endif %}
</body>
</html>
"""

def get_cfg():
    return {
        "model": os.getenv("AI_MODEL", config.AI_MODEL),
        "base": os.getenv("AI_BASE_URL", config.AI_BASE_URL),
        "triggers": os.getenv("AI_TRIGGER_NAMES", getattr(config, "AI_TRIGGER_NAMES_RAW", "")),
        "prompt": os.getenv("AI_SYSTEM_PROMPT", config.AI_SYSTEM_PROMPT),
    }

@app.route("/", methods=["GET"])
def index():
    logged = session.get("logged")
    cfg = get_cfg()
    # also allow runtime overrides stored in env
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
    if base: os.environ["AI_BASE_URL"]=base
    os.environ["AI_TRIGGER_NAMES"]=triggers
    if prompt: os.environ["AI_SYSTEM_PROMPT"]=prompt
    # пытаемся сохранить в .env файл для локалки
    try:
        env_path = ".env"
        if os.path.exists(env_path):
            with open(env_path,"r",encoding="utf-8") as f: content=f.read()
            def upsert(key,val):
                nonlocal content
                import re
                if re.search(rf"^{key}=.*", content, flags=re.M):
                    content=re.sub(rf"^{key}=.*", f"{key}={val}", content, flags=re.M)
                else:
                    content+=f"\n{key}={val}"
            if model: upsert("AI_MODEL", model)
            if base: upsert("AI_BASE_URL", base)
            upsert("AI_TRIGGER_NAMES", triggers)
            # промпт многострочный — экранируем \n
            if prompt:
                # пишем как одну строку с \n замещаем
                safe=prompt.replace("\n","\\n").replace('"','\\"')
                # если уже есть — заменим
                if "AI_SYSTEM_PROMPT=" in content:
                    content=re.sub(r"AI_SYSTEM_PROMPT=.*", f'AI_SYSTEM_PROMPT={safe}', content)
                else:
                    content+=f"\nAI_SYSTEM_PROMPT={safe}"
            with open(env_path,"w",encoding="utf-8") as f: f.write(content)
    except Exception as e:
        print("panel save .env fail", e)
    return redirect("/?msg=Сохранено! Бот уже использует новый промпт&ok=1")

if __name__=="__main__":
    port=int(os.getenv("PORT","8080"))
    app.run(host="0.0.0.0", port=port)
