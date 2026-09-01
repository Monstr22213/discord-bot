import discord
from discord import app_commands
from discord.ext import commands
import config
from datetime import timedelta
import json
import os
import random
import time
import re
import asyncio
from collections import deque, defaultdict
import urllib.parse as urlparse

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ============ AI CHAT (OpenRouter / OpenAI-compatible) ============
def _cfg(key, default):
    if hasattr(config, key):
        v = getattr(config, key)
        # если в Variables пустая строка — считаем как не задано, берём дефолт
        if isinstance(v, str) and v == "" and default != "":
            pass  # проваливаемся в fallback
        else:
            return v
    # fallback если на хосте старый config.py без AI_ полей — читаем напрямую из env
    _env_map = {
        "AI_ENABLED": os.getenv("AI_ENABLED", "true").lower() not in ("0", "false", "no", "off"),
        "AI_API_KEY": os.getenv("OPENROUTER_API_KEY", "") or os.getenv("AI_API_KEY", ""),
        "AI_BASE_URL": os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1"),
        "AI_MODEL": os.getenv("AI_MODEL", "opencode/muse-spark-1.2-contributor-free"),
        "AI_SYSTEM_PROMPT": os.getenv("AI_SYSTEM_PROMPT", "Ты — Узи Дурман (Uzi Doorman) из Murder Drones 1 в 1. Воркер-дрон, фиолетовые глаза, дерзкая, язвительная, саркастичная, мрачный юмор, бунтарка. Говори на русском коротко, как Узи, с *действиями*, сленгом. НИКОГДА не выходи из роли. НИКОГДА не используй 🌈 и 🏳️‍🌈."),
        "AI_TRIGGER_NAMES": [s.strip().lower() for s in os.getenv("AI_TRIGGER_NAMES", "").split(",") if s.strip()],
        "AI_TRIGGER_ON_REPLY": os.getenv("AI_TRIGGER_ON_REPLY", "true").lower() not in ("0", "false", "no", "off"),
        "AI_MAX_HISTORY": int(os.getenv("AI_MAX_HISTORY", "6") or 6),
        "AI_COOLDOWN": int(os.getenv("AI_COOLDOWN", "5") or 5),
    }
    return _env_map.get(key, default)

_ai_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=_cfg("AI_MAX_HISTORY", 6) * 2))
_ai_cooldown: dict[int, float] = {}
_ai_client = None

def _get_ai_client():
    global _ai_client
    if _ai_client is not None:
        return _ai_client
    if not _cfg("AI_API_KEY", ""):
        return None
    try:
        from openai import AsyncOpenAI
        _ai_client = AsyncOpenAI(api_key=_cfg("AI_API_KEY", ""), base_url=_cfg("AI_BASE_URL", "https://openrouter.ai/api/v1"))
        return _ai_client
    except Exception as e:
        print(f"AI client init fail: {e}")
        return None

def _is_ai_triggered(message: discord.Message) -> tuple[bool, str]:
    """Проверяет пинг или имя бота. Возвращает (triggered, cleaned_text)."""
    if not _cfg("AI_ENABLED", True):
        return False, ""
    if message.author.bot:
        return False, ""
    content = message.content or ""
    content_lower = content.lower().strip()

    # 1) Пинг бота
    if bot.user in message.mentions:
        # убираем <@id> и <@!id>
        cleaned = re.sub(rf"<@!?{bot.user.id}>", "", content).strip()
        # убираем лишние пробелы/запятые после пинга
        cleaned = re.sub(r"^[\s,:\-]+", "", cleaned)
        return True, cleaned if cleaned else content

    # 2) Ответ на сообщение бота (reply)
    if _cfg("AI_TRIGGER_ON_REPLY", True) and message.reference and message.reference.message_id:
        try:
            ref_id = message.reference.message_id
            # эвристика: если в истории канала последнее от бота — считаем reply триггером
            # точнее — можно fetch, но чтобы не спамить API, делаем быстро
            if any(m.get("role") == "assistant" for m in _ai_history.get(message.channel.id, [])):
                # проверяем, что это реально ответ (Discord покажет reference)
                return True, content
        except:
            pass

    # 3) Имя бота в начале сообщения (или где угодно, если имя отдельное слово)
    # Собираем триггеры: из .env + имя бота + display_name + username + алиасы Узи
    triggers = set(_cfg("AI_TRIGGER_NAMES", []))
    # алиасы чтобы после переименования в Узи не пропустить
    triggers.update(["узи", "uzi", "узи дурман", "uzi doorman", "анечка", "анечка-бот", "бот"])
    if bot.user:
        triggers.add(bot.user.name.lower())
        triggers.add(bot.user.display_name.lower())
        # без дискриминатора
        triggers.add(bot.user.name.lower().split("#")[0])
    # убираем пустые
    triggers = {t for t in triggers if t}
    if not triggers:
        return False, ""

    # проверяем в начале: "мила привет" или "бот, как дела?" или "ice помоги"
    for name in triggers:
        # в начале сообщения
        if content_lower.startswith(name):
            # отрезаем имя + возможный разделитель , : - 
            pattern = re.compile(rf"^{re.escape(name)}[\s,:\-]*", re.IGNORECASE)
            cleaned = pattern.sub("", content, count=1).strip()
            if cleaned:
                return True, cleaned
            else:
                # просто "мила" без текста — тоже триггер, но вернем пусто чтобы бот спросил что надо
                return True, ""
        # упоминание имени как отдельного слова где угодно: "привет мила как дела"
        # чтобы не триггерить на каждое "бот" в середине, требуем чтобы слово стояло отдельно и сообщение короткое
        # оставим только "в начале" для точности, чтобы не спамить

    return False, ""

async def _ask_ai(prompt: str, channel_id: int, author_name: str, author_id: int = 0, guild_name: str = "") -> str:
    api_key = _cfg("AI_API_KEY", "")
    base_url = _cfg("AI_BASE_URL", "https://opencode.ai/zen/v1/responses")
    model = _cfg("AI_MODEL", "muse-spark-1.2-contributor-free")
    if not api_key:
        return "❌ AI не настроен: добавь `OPENROUTER_API_KEY` или `OPENCODE_API_KEY` в Variables на Railway"
    # история канала
    hist = _ai_history[channel_id]
    base_prompt = _cfg("AI_SYSTEM_PROMPT", "Ты — дружелюбный бот. Отвечай на русском, коротко.")
    ctx = f"\n\n[Контекст: тебя зовут Анечка=Узи. Сейчас пишет '{author_name}' (ID {author_id}) на сервере '{guild_name}'. Формат 'Автор: текст' — АВТОР кто написал, @упоминания — ДРУГИЕ. Пример: 'Serial Designation V: Узи, @hanacoamilla сказал...' — автор V, hanacoamilla — третий. Лор: 'N' — добрый краш, мягко-цундере; 'V/Vi' — дерзкая подруга, на равных; 'J' — враждебно; 'Syn' — НЕНАВИДИШЬ (пыталась убить N) 'Syn, тронешь N — разнесу!'; 'Yeva/Йева' — подруга, тепло-дружелюбно 'о, Yeva, привет!'; Khan — бурчи; остальные типа hanacoamilla — обычные воркеры, нейтрально-язвительно. Обращайся по имени автора.]"
    # если это opencode zen — используем Responses API как в sv_lotm_ai.lua
    is_opencode = "opencode.ai" in base_url
    try:
        if is_opencode:
            # собираем input как в LotM: история + текущий вопрос
            hist_text = "\n".join([f"{m['role']}: {m['content']}" for m in hist]) if hist else ""
            full_input = (hist_text + "\n" if hist_text else "") + f"{author_name}: {prompt}"
            # если есть ctx — добавляем через instructions
            instructions = base_prompt + ctx
            import aiohttp
            payload = {
                "model": model if "/" not in model else model.split("/")[-1] if model.startswith("opencode/") else model,
                "instructions": instructions,
                "input": full_input,
                "temperature": 0.9,
                "max_output_tokens": 800,
                "reasoning": {"effort": "low"}
            }
            # opencode ожидает чистый ID без префикса opencode/
            if payload["model"].startswith("muse-spark"):
                payload["model"] = "muse-spark-1.2-contributor-free"
            async with aiohttp.ClientSession() as sess:
                async with sess.post(base_url, json=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "HTTP-Referer": "https://discord-bot.local", "X-Title": "Discord Uzi Bot"}, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    txt = await r.text()
                    if r.status != 200:
                        raise Exception(f"HTTP {r.status}: {txt[:400]}")
                    import json as _json
                    data = _json.loads(txt)
                    # парсим как в lua: output[0].content[0].text или choices
                    ans = None
                    if data.get("choices") and data["choices"][0].get("message"):
                        ans = data["choices"][0]["message"]["content"]
                    elif data.get("output"):
                        for out in data["output"]:
                            if out.get("content"):
                                for c in out["content"]:
                                    if c.get("text"):
                                        ans = c["text"]
                                        break
                            if ans: break
                    ans = ans or data.get("output_text") or data.get("text") or data.get("response") or ""
                    text = str(ans).strip() or "Туман молчит..."
        else:
            client = _get_ai_client()
            if not client:
                return "❌ AI не настроен (нет клиента)"
            messages = [{"role": "system", "content": base_prompt + ctx}]
            for m in hist:
                messages.append(m)
            messages.append({"role": "user", "content": f"{author_name}: {prompt}"})
            async def _call(m):
                return await client.chat.completions.create(model=m, messages=messages, max_tokens=800, temperature=0.8, timeout=25)
            try:
                resp = await asyncio.wait_for(_call(model), timeout=30)
            except Exception as e1:
                if "404" in str(e1) and model != "muse-spark-1.2-contributor-free":
                    print(f"AI model {model} 404, retry muse-spark-1.2-contributor-free")
                    resp = await asyncio.wait_for(_call("muse-spark-1.2-contributor-free"), timeout=30)
                else:
                    raise
            text = resp.choices[0].message.content.strip()
        # фильтр радуги
        text = text.replace("🌈", "").replace("🏳️‍🌈", "").replace("🏳️\u200d🌈", "")
        hist.append({"role": "user", "content": f"{author_name}: {prompt}"})
        hist.append({"role": "assistant", "content": text})
        if len(text) > 1900:
            text = text[:1900] + "…"
        return text
    except Exception as e:
        err = str(e)
        print(f"AI error: {err}")
        if "401" in err or "auth" in err.lower():
            return "❌ Ошибка ключа — проверь OPENCODE_API_KEY/OPENROUTER_API_KEY в Variables"
        if "429" in err:
            return "⏳ AI перегружен (429), попробуй через минуту"
        return f"❌ Ошибка AI: {err[:400]}"

# ============ МУЗЫКА (Анечка зайди / включи) ============
_music_queues: dict[int, deque] = defaultdict(deque)  # guild_id -> deque of {url, title, requester}
_now_playing: dict[int, dict] = {}

FFMPEG_OPTIONS = {"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 200K -analyzeduration 0", "options": "-vn -b:a 128k -ar 48000"}

def _music_is_anechka(text_lower: str) -> bool:
    # чтобы "анечка" срабатывала и с "анечка," "анечка " и если пинг
    return "анечка" in text_lower or "анечка" in text_lower.replace("ё","е")

async def _music_join(msg: discord.Message) -> bool:
    if not msg.author.voice or not msg.author.voice.channel:
        await msg.reply("🚁 Зайди сначала в голосовой канал, брат! *хик* — я не знаю куда лететь.", mention_author=False)
        return False
    channel = msg.author.voice.channel
    vc = msg.guild.voice_client
    try:
        if vc and vc.is_connected():
            if vc.channel.id == channel.id:
                await msg.reply(f"🚁 Я уже тут, в `{channel.name}` кручу винтами! 💅", mention_author=False)
                return True
            await vc.move_to(channel)
        else:
            await channel.connect(self_deaf=False)
        await msg.reply(f"🚁 *влетаю* в `{channel.name}`! Скажи `Анечка включи <песня>` — и я заведу пластинку! 💿", mention_author=False)
        return True
    except Exception as e:
        await msg.reply(f"❌ Не смогла зайти: {e}", mention_author=False)
        return False

async def _music_leave(msg: discord.Message):
    vc = msg.guild.voice_client
    if not vc or not vc.is_connected():
        await msg.reply("Я и так не в войсе 😅", mention_author=False)
        return
    _music_queues[msg.guild.id].clear()
    _now_playing.pop(msg.guild.id, None)
    await vc.disconnect()
    await msg.reply("🚁 *улетаю*... пока, котик! 💋", mention_author=False)

def _music_play_next(guild: discord.Guild):
    q = _music_queues[guild.id]
    vc = guild.voice_client
    if not vc or not q:
        _now_playing.pop(guild.id, None)
        return
    item = q.popleft()
    _now_playing[guild.id] = item
    try:
        # yt-dlp уже дал прямой url
        source = discord.FFmpegOpusAudio(item["url"], **FFMPEG_OPTIONS)
        def after(err):
            if err:
                print(f"music after error: {err}")
            # следующий трек в loop
            bot.loop.call_soon_threadsafe(lambda: _music_play_next(guild))
        vc.play(source, after=after)
        # анонс в текстовый канал кэшируем
        ch_id = item.get("channel_id")
        if ch_id:
            ch = bot.get_channel(ch_id)
            if ch:
                bot.loop.create_task(ch.send(f"🎧 Сейчас играет: **{item['title']}** — заказал {item['requester']}"))
    except Exception as e:
        print(f"music play_next fail: {e}")
        bot.loop.call_soon_threadsafe(lambda: _music_play_next(guild))

async def _music_enqueue(msg: discord.Message, query: str):
    if not query:
        await msg.reply("Скажи что включить: `Анечка включи <название или ссылка>` 🎶", mention_author=False)
        return
    vc = msg.guild.voice_client
    if not vc or not vc.is_connected():
        ok = await _music_join(msg)
        if not ok:
            return
        vc = msg.guild.voice_client
    # ищем через yt-dlp
    await msg.channel.typing()
    try:
        import yt_dlp
    except ImportError:
        await msg.reply("❌ yt-dlp не установлен на хосте. Добавь в requirements и пересобери.", mention_author=False)
        return
    # YouTube с Railway IP просит куки — пробуем несколько клиентов, + YT_COOKIES если есть
    base_ydl_opts = {"format": "bestaudio/best", "noplaylist": True, "quiet": True, "no_warnings": True, "default_search": "ytsearch1", "extract_flat": False, "skip_download": True, "nocheckcertificate": True, "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}}
    # готовим набор клиентов для обхода (android обходит Sign in)
    client_sets = [["android","web"], ["android_music","android"], ["ios","android","web"], ["web"]]
    import tempfile
    cookies_data = os.getenv("YT_COOKIES", "")
    ydl_opts = base_ydl_opts.copy()
    if cookies_data and "netscape" in cookies_data.lower():
        try:
            tf = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
            tf.write(cookies_data)
            tf.close()
            ydl_opts["cookiefile"] = tf.name
        except:
            pass
        client_sets = [["web"]]  # с куками web лучше
    loop = asyncio.get_event_loop()
    def _extract():
        last_err = None
        for clients in client_sets:
            opts = ydl_opts.copy()
            opts["extractor_args"] = {"youtube": {"player_client": clients}}
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    if query.startswith("http"):
                        info = ydl.extract_info(query, download=False)
                    else:
                        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
                        if "entries" in info:
                            info = info["entries"][0] if info["entries"] else None
                    return info
            except Exception as e:
                last_err = e
                if "Sign in" in str(e) or "cookies" in str(e).lower():
                    continue
                raise
        if last_err:
            raise last_err
        return None
    try:
        info = await loop.run_in_executor(None, _extract)
    except Exception as e:
        await msg.reply(f"❌ Не нашла: {e}", mention_author=False)
        return
    if not info:
        await msg.reply("❌ Ничего не нашла по запросу.", mention_author=False)
        return
    # прямой аудио url
    url = info.get("url") or info.get("entries", [{}])[0].get("url")
    # для некоторых экстракторов url в formats
    if not url and info.get("formats"):
        url = info["formats"][-1].get("url")
    if not url:
        # пробуем взять webpage fallback
        url = info.get("webpage_url") or query
        # если это веб-страница — не сыграет, сообщаем
        await msg.reply(f"❌ Не смогла вытащить аудио. Попробуй прямую ссылку на YouTube.", mention_author=False)
        return
    title = info.get("title", query)[:150]
    item = {"url": url, "title": title, "requester": msg.author.mention, "channel_id": msg.channel.id, "webpage": info.get("webpage_url", "")}
    _music_queues[msg.guild.id].append(item)
    vc = msg.guild.voice_client
    if vc.is_playing() or vc.is_paused():
        await msg.reply(f"✅ В очередь: **{title}** (#{len(_music_queues[msg.guild.id])})", mention_author=False)
    else:
        await msg.reply(f"🔍 Нашла **{title}** — врубаю! 🚁💿", mention_author=False)
        _music_play_next(msg.guild)

async def _handle_music_triggers(message: discord.Message) -> bool:
    """Возвращает True если это музыкальная команда и уже обработана (не надо в AI). Поддерживает Анечка/Узи/Uzi."""
    if not message.guild or message.author.bot:
        return False
    low = message.content.lower().strip()
    # убираем пинг в начале
    low_clean = re.sub(rf"<@!?{bot.user.id}>" if bot.user else r"<@!?\d+>", "", low).strip() if bot.user else low
    def _start_any(names, *suffixes):
        for n in names:
            for s in suffixes:
                if low_clean.startswith(f"{n} {s}") or low_clean == f"{n} {s}":
                    return True
        return False
    names = ["анечка", "узи", "uzi"]
    # зайди
    if _start_any(names, "зайди", "зайди ко мне", "зайди к нам", "го в войс", "го к нам", "присоединись", "зайди в войс"):
        await _music_join(message)
        return True
    if _start_any(names, "выйди", "ливни", "ливнуть", "ливать", "покинь", "уйди", "выйди из войса", "ливни из войса"):
        await _music_leave(message)
        return True
    # умные музыкальные триггеры: "узи включи/поставь/добавь/запусти/в очередь/плей"
    if any(low_clean.startswith(n) for n in names) and any(kw in low_clean for kw in ["включи","поставь","добавь","запусти","очередь","плей","play"]):
        q = re.sub(r"^(?:анечка|узи|uzi)\s+(?:включи|поставь(?:\s+в\s+очередь)?|добавь(?:\s+в\s+очередь)?|запусти|плей|play)\s*", "", message.content, flags=re.I).strip()
        q = re.sub(r"^(?:в\s+очередь\s*)", "", q, flags=re.I).strip()
        q = re.sub(rf"<@!?{bot.user.id}>", "", q).strip() if bot.user else q
        if q and len(q) > 2:
            await _music_enqueue(message, q)
            return True
        # если запрос пустой но явно музыка — не кидаем в AI
        await _music_enqueue(message, q)
        return True
    if _start_any(names, "стоп", "пауза"):
        vc = message.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await message.reply("⏸️ Пауза, котик... *винты замедляются* 🚁", mention_author=False)
        else:
            await message.reply("Нечего ставить на паузу.", mention_author=False)
        return True
    if _start_any(names, "продолжи", "резюме", "play"):
        vc = message.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await message.reply("▶️ Поехали дальше! 🚁💨", mention_author=False)
        else:
            await message.reply("Нечего продолжать.", mention_author=False)
        return True
    if _start_any(names, "скип", "дальше", "пропусти", "next", "скипни"):
        vc = message.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()  # after вызовет следующий
            await message.reply("⏭️ Скипаю... 🚁", mention_author=False)
        else:
            await message.reply("Очередь пуста.", mention_author=False)
        return True
    if _start_any(names, "очередь", "что играет", "что сейчас играет"):
        q = _music_queues[message.guild.id]
        now = _now_playing.get(message.guild.id)
        desc = ""
        if now:
            desc += f"▶️ Сейчас: **{now['title']}**\n"
        if q:
            desc += "\n".join([f"{i}. {it['title']}" for i, it in enumerate(list(q)[:10], 1)])
        else:
            desc += "Очередь пуста."
        await message.reply(desc or "Тихо...", mention_author=False)
        return True
    # === Нейронка для понимания контекста (если не сработал готовый шаблон) ===
    # Если сообщение начинается с имени (узи/анечка/uzi) и не попало выше — спрашиваем у Muse Spark что хотел пользователь
    if any(low_clean.startswith(n) for n in names):
        try:
            # быстрая классификация через Opencode (1-2 сек), без истории
            query = low_clean
            for n in names:
                if query.startswith(n):
                    query = query[len(n):].lstrip(" ,:-")
                    break
            if len(query) < 3:
                return False
            # эвристика: если в тексте есть намек на музыку — дергаем нейронку
            music_hints = ["музы", "песн", "трек", "саунд", "звук", "включи", "постав", "очеред", "плей", "play", "youtube", "youtu.be", "soundcloud", "spotify"]
            if not any(h in query.lower() for h in music_hints) and len(query) < 15:
                return False
            api_key = _cfg("AI_API_KEY", "")
            base_url = _cfg("AI_BASE_URL", "https://opencode.ai/zen/v1/responses")
            if not api_key or "opencode.ai" not in base_url:
                return False
            import aiohttp, json as _json
            payload = {
                "model": "muse-spark-1.2-contributor-free",
                "instructions": "Ты классификатор. Пользователь пишет боту Узи. Определи хочет ли он включить/добавить музыку/звук в очередь в войсе. Если да — верни ТОЛЬКО запрос для поиска (название песни/ссылку) без лишних слов. Если нет — верни ровно NO. Примеры: 'поставь в очередь Обормот Gay ремикс' -> 'Обормот Gay ремикс', 'можешь врубить что нибудь из дронов убийц?' -> 'дронов убийц', 'как дела?' -> NO",
                "input": query,
                "temperature": 0.2,
                "max_output_tokens": 60
            }
            async with aiohttp.ClientSession() as sess:
                async with sess.post(base_url, json=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status != 200:
                        return False
                    data = await r.json()
                    ans = None
                    if data.get("output"):
                        for out in data["output"]:
                            if out.get("content"):
                                for c in out["content"]:
                                    if c.get("text"):
                                        ans = c["text"]
                                        break
                            if ans: break
                    ans = (ans or data.get("output_text") or "").strip()
                    if not ans or ans.upper() == "NO" or len(ans) < 2 or "NO" in ans.upper() and len(ans) < 6:
                        return False
                    # нейронка сказала что это музыка — включаем
                    ans = ans.replace('"','').replace("'","").strip()
                    if len(ans) > 120:
                        ans = ans[:120]
                    await _music_enqueue(message, ans)
                    return True
        except Exception as e:
            print(f"ai music intent fail: {e}")
            return False
    return False

# ============ ЭКОНОМИКА СПЕРМИКИ (сохранение) ============
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PRIVATE_URL")

def get_db():
    url = DATABASE_URL or os.getenv("DATABASE_URL") or os.getenv("DATABASE_PRIVATE_URL")
    if not url:
        return None
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=5, sslmode="require")
        conn.autocommit = True
        return conn
    except Exception as e:
        print(f"DB connect fail: {e}")
        return None

def init_db():
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS economy (user_id TEXT PRIMARY KEY, balance INT DEFAULT 0, daily BIGINT DEFAULT 0, items TEXT DEFAULT '[]')")
        cur.execute("CREATE TABLE IF NOT EXISTS quest (guild_id TEXT PRIMARY KEY, data TEXT)")
        cur.execute("CREATE TABLE IF NOT EXISTS promo_codes (code TEXT PRIMARY KEY, reward INT NOT NULL, max_uses INT DEFAULT 1, uses INT DEFAULT 0, created_by TEXT, created_at BIGINT)")
        cur.execute("CREATE TABLE IF NOT EXISTS promo_uses (code TEXT, user_id TEXT, PRIMARY KEY(code, user_id))")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB init fail: {e}")

DATA_DIR = "/data" if os.path.exists("/data") else "."
ECONOMY_FILE = os.path.join(DATA_DIR, "economy.json")
QUEST_FILE = os.path.join(DATA_DIR, "quest_dima.json")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except:
    pass

def load_economy():
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT user_id, balance, daily, items FROM economy")
            rows = cur.fetchall()
            data = {}
            for uid, bal, daily, items in rows:
                try:
                    it = json.loads(items) if items else []
                except:
                    it = []
                data[uid] = {"balance": bal, "daily": daily, "items": it}
            cur.close()
            conn.close()
            return data
        except:
            try:
                conn.close()
            except:
                pass
    if not os.path.exists(ECONOMY_FILE):
        return {}
    try:
        with open(ECONOMY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_economy(data):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            for uid, info in data.items():
                bal = info.get("balance", 0)
                daily = int(info.get("daily", 0))
                items = json.dumps(info.get("items", []), ensure_ascii=False)
                cur.execute("INSERT INTO economy (user_id, balance, daily, items) VALUES (%s,%s,%s,%s) ON CONFLICT (user_id) DO UPDATE SET balance=EXCLUDED.balance, daily=EXCLUDED.daily, items=EXCLUDED.items", (uid, bal, daily, items))
            cur.close()
            conn.close()
            return
        except Exception as e:
            print(f"save_economy DB fail: {e}")
            try:
                conn.close()
            except:
                pass
    try:
        with open(ECONOMY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass

def get_balance(user_id: int):
    data = load_economy()
    return data.get(str(user_id), {}).get("balance", 0)

def add_spermi(user_id: int, amount: int):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            uid = str(user_id)
            cur.execute("INSERT INTO economy (user_id, balance) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (uid, 0))
            cur.execute("UPDATE economy SET balance = GREATEST(0, balance + %s) WHERE user_id=%s RETURNING balance", (amount, uid))
            bal = cur.fetchone()[0]
            cur.close()
            conn.close()
            return bal
        except Exception as e:
            print(f"add_spermi DB fail: {e}")
            try:
                conn.close()
            except:
                pass
    data = load_economy()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"balance": 0, "daily": 0, "items": []}
    data[uid]["balance"] = data[uid].get("balance", 0) + amount
    if data[uid]["balance"] < 0:
        data[uid]["balance"] = 0
    save_economy(data)
    return data[uid]["balance"]

def nuke_balance(user_id: int):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("UPDATE economy SET balance=0 WHERE user_id=%s", (str(user_id),))
            cur.close()
            conn.close()
            return 0
        except:
            pass
    data = load_economy()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"balance": 0, "daily": 0, "items": []}
    data[uid]["balance"] = 0
    save_economy(data)
    return 0

def ensure_db():
    try:
        init_db()
        conn = get_db()
        if conn:
            print("DB: connected OK, tables ensured")
            conn.close()
        else:
            print("DB: no connection, fallback to file")
    except Exception as e:
        print(f"DB ensure fail: {e}")

try:
    ensure_db()
except:
    pass

# Магазин
SHOP_ITEMS = {
    "бронь_от_дрона": {"price": 500, "desc": "Защита от дроноеба на 1 день", "role": None},
    "раб_дроноеб": {"price": 777, "desc": "Роль Раб дроноеб 🚁", "role": "Раб дроноеб"},
    "vip_спермик": {"price": 300, "desc": "VIP роль + цвет", "role": "VIP Спермик"},
    "цвет_ника": {"price": 250, "desc": "Смена цвета ника (выбери цвет после покупки)", "role": "Цветной"},
}

# ============ VIEWS ============
def get_verify_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="бусифицированный")
    if role:
        return role
    if config.VERIFY_ROLE_ID:
        return guild.get_role(config.VERIFY_ROLE_ID)
    return None

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="🚐 Пройти Бусификацию", style=discord.ButtonStyle.green, custom_id="verify_btn")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = get_verify_role(interaction.guild)
        if not role:
            await interaction.response.send_message("❌ Роль `бусифицированный` не найдена. Напиши `/основа` чтобы создать.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message(f"Ты уже верифицирован! Роль {role.mention} уже есть.", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Верификация через кнопку")
            await interaction.response.send_message(f"✅ Успешно! Тебе выдана роль {role.mention}", ephemeral=True)
            if config.LOG_CHANNEL_ID:
                ch = bot.get_channel(config.LOG_CHANNEL_ID)
                if ch:
                    await ch.send(f"✅ {interaction.user.mention} прошел верификацию")
        except discord.Forbidden:
            await interaction.response.send_message("❌ У меня нет прав выдать роль. Проверь иерархию ролей бота.", ephemeral=True)

class RolesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        if config.SELF_ROLES:
            self.add_item(RolesSelect())

class RolesSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for role_id in config.SELF_ROLES[:25]:
            options.append(discord.SelectOption(label=f"Роль {role_id}", value=str(role_id), description="Нажми чтобы получить/снять"))
        super().__init__(placeholder="Выбери роли...", min_values=0, max_values=len(options) if options else 1, options=options if options else [discord.SelectOption(label="Нет ролей", value="0")], custom_id="roles_select")
    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        selected_ids = set(int(v) for v in self.values)
        added = []
        removed = []
        for role_id in config.SELF_ROLES:
            role = guild.get_role(role_id)
            if not role:
                continue
            has_role = role in member.roles
            should_have = role_id in selected_ids
            try:
                if should_have and not has_role:
                    await member.add_roles(role)
                    added.append(role.name)
            except discord.Forbidden:
                await interaction.response.send_message("❌ Нет прав на выдачу ролей.", ephemeral=True)
                return
        for role_id in config.SELF_ROLES:
            role = guild.get_role(role_id)
            if role and role in member.roles and role_id not in selected_ids:
                try:
                    await member.remove_roles(role)
                    removed.append(role.name)
                except:
                    pass
        msg = []
        if added: msg.append(f"✅ Выдано: {', '.join(added)}")
        if removed: msg.append(f"❌ Снято: {', '.join(removed)}")
        if not msg:
            msg.append("Без изменений.")
        await interaction.response.send_message("\n".join(msg), ephemeral=True)

class RolesButtonView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        for role_id in config.SELF_ROLES[:25]:
            role = guild.get_role(role_id)
            if role:
                self.add_item(RoleButton(role))

class RoleButton(discord.ui.Button):
    def __init__(self, role: discord.Role):
        super().__init__(label=role.name, style=discord.ButtonStyle.secondary, custom_id=f"role_{role.id}")
        self.role_id = role.id
    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            await interaction.response.send_message("Роль не найдена", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message(f"❌ Роль {role.mention} снята", ephemeral=True)
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(f"✅ Роль {role.mention} выдана", ephemeral=True)

class TransferModal(discord.ui.Modal, title="Перевести спермики"):
    target = discord.ui.TextInput(label="Кому (ID или @упоминание)", placeholder="123456789 или @user", required=True, max_length=30)
    amount = discord.ui.TextInput(label="Сколько спермиков", placeholder="100", required=True, max_length=6)
    async def on_submit(self, interaction: discord.Interaction):
        import re
        m = re.search(r"\d{15,}", self.target.value.strip())
        if not m:
            await interaction.response.send_message("❌ Укажи ID или упомяни пользователя", ephemeral=True)
            return
        tid = int(m.group(0))
        try:
            amt = int(self.amount.value.strip())
        except:
            await interaction.response.send_message("❌ Неверное количество", ephemeral=True)
            return
        if amt <= 0 or amt > 10000:
            await interaction.response.send_message("❌ От 1 до 10000", ephemeral=True)
            return
        if tid == interaction.user.id:
            await interaction.response.send_message("❌ Себе нельзя", ephemeral=True)
            return
        if get_balance(interaction.user.id) < amt:
            await interaction.response.send_message(f"❌ Недостаточно. Баланс: {get_balance(interaction.user.id)}", ephemeral=True)
            return
        add_spermi(interaction.user.id, -amt)
        add_spermi(tid, amt)
        member = interaction.guild.get_member(tid) if interaction.guild else None
        name = member.mention if member else f"<@{tid}>"
        await interaction.response.send_message(f"💸 Перевел {amt} 💦 → {name}. Баланс: {get_balance(interaction.user.id)}", ephemeral=True)

class PromoModal(discord.ui.Modal, title="Ввести промокод"):
    code = discord.ui.TextInput(label="Промокод", placeholder="SUPER2026", required=True, max_length=20)
    async def on_submit(self, interaction: discord.Interaction):
        code = self.code.value.strip().upper()
        conn = get_db()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT reward, max_uses, uses FROM promo_codes WHERE code=%s", (code,))
                row = cur.fetchone()
                if not row:
                    await interaction.response.send_message("❌ Неверный промокод", ephemeral=True)
                    cur.close(); conn.close()
                    return
                reward, max_uses, uses = row
                cur.execute("SELECT 1 FROM promo_uses WHERE code=%s AND user_id=%s", (code, str(interaction.user.id)))
                if cur.fetchone():
                    await interaction.response.send_message("❌ Ты уже использовал этот промокод", ephemeral=True)
                    cur.close(); conn.close()
                    return
                if uses >= max_uses:
                    await interaction.response.send_message("❌ Промокод закончился", ephemeral=True)
                    cur.close(); conn.close()
                    return
                cur.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=%s", (code,))
                cur.execute("INSERT INTO promo_uses (code, user_id) VALUES (%s,%s)", (code, str(interaction.user.id)))
                cur.execute("INSERT INTO economy (user_id, balance) VALUES (%s,0) ON CONFLICT (user_id) DO NOTHING", (str(interaction.user.id),))
                cur.execute("UPDATE economy SET balance = balance + %s WHERE user_id=%s", (reward, str(interaction.user.id)))
                cur.close(); conn.close()
                await interaction.response.send_message(f"✅ Промокод активирован! +{reward} 💦", ephemeral=True)
                return
            except Exception as e:
                try:
                    conn.close()
                except:
                    pass
                await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)
                return
        await interaction.response.send_message("❌ База промокодов не настроена (нет Postgres)", ephemeral=True)

# ============ ПРОФИЛЬ / МАГАЗИН ПАНЕЛИ ============
class ProfileView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Открыть инвентарь", style=discord.ButtonStyle.secondary, custom_id="profile_inv", emoji="📦", row=0)
    async def inv(self, interaction: discord.Interaction, button: discord.ui.Button):
        bal = get_balance(interaction.user.id)
        data = load_economy()
        inv = data.get(str(interaction.user.id), {}).get("items", [])
        count = len(inv)
        if count == 0:
            embed = discord.Embed(title="Инвентарь", description="*Инвентарь пуст*\nПредметов: 0", color=discord.Color.from_rgb(255, 107, 139))
        else:
            embed = discord.Embed(title="Инвентарь", description=f"Предметов: {count}\n" + "\n".join([f"• {x}" for x in inv]), color=discord.Color.from_rgb(255, 107, 139))
            embed.add_field(name="💦 Спермики", value=str(bal), inline=True)
        embed.set_footer(text="Только вы видите это сообщение")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    @discord.ui.button(label="Выбрать роли", style=discord.ButtonStyle.secondary, custom_id="profile_roles", emoji="🎨", row=0)
    async def roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if not config.SELF_ROLES:
            await interaction.response.send_message("Роли не настроены", ephemeral=True)
            return
        view = RolesButtonView(guild)
        await interaction.response.send_message("Выбери роли:", view=view, ephemeral=True)
    @discord.ui.button(label="Ежедневка +100", style=discord.ButtonStyle.success, custom_id="profile_daily", emoji="💰", row=0)
    async def daily_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_economy()
        uid = str(interaction.user.id)
        now = time.time()
        last = data.get(uid, {}).get("daily", 0)
        if now - last < 86400:
            left = 86400 - (now - last)
            h = int(left // 3600)
            m = int((left % 3600) // 60)
            await interaction.response.send_message(f"⏳ Уже брал! Через {h}ч {m}м", ephemeral=True)
            return
        bal = add_spermi(interaction.user.id, 100)
        data = load_economy()
        data[uid]["daily"] = now
        save_economy(data)
        await interaction.response.send_message(f"✅ Получено 100 💦! Баланс: {bal}", ephemeral=True)
    @discord.ui.button(label="Перевести", style=discord.ButtonStyle.secondary, custom_id="profile_transfer", emoji="💸", row=1)
    async def transfer_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TransferModal())
    @discord.ui.button(label="Рулетка дроноеба", style=discord.ButtonStyle.danger, custom_id="profile_roulete", emoji="🚁", row=1)
    async def roulete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if get_balance(interaction.user.id) < 50:
            await interaction.response.send_message(f"❌ Нужно 50 💦, у тебя {get_balance(interaction.user.id)}", ephemeral=True)
            return
        roll = random.randint(1, 100)
        if roll == 1:
            old = get_balance(interaction.user.id)
            nuke_balance(interaction.user.id)
            await interaction.response.send_message(f"☢️ **ЯДЕРКА УПАЛА!** Ролл `1/100` — ядерка раба дроноеба! Все **{old} 💦** сгорели! Баланс: 0", ephemeral=True)
            return
        if roll <= 45:
            add_spermi(interaction.user.id, -50)
            await interaction.response.send_message(f"💥 Раб дроноеб ебанул! -50 💦. Ролл {roll}/100", ephemeral=True)
        elif roll <= 90:
            win = 75
            add_spermi(interaction.user.id, win)
            await interaction.response.send_message(f"🚁 Промах! +{win} 💦! Ролл {roll}/100. Баланс: {get_balance(interaction.user.id)}", ephemeral=True)
        else:
            win = 150
            add_spermi(interaction.user.id, win)
            await interaction.response.send_message(f"🔥 ДЖЕКПОТ! +{win} 💦! Ролл {roll}/100. Баланс: {get_balance(interaction.user.id)}", ephemeral=True)
    @discord.ui.button(label="Ввести промокод", style=discord.ButtonStyle.secondary, custom_id="profile_promo", emoji="🎟️", row=2)
    async def promo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PromoModal())
    @discord.ui.button(label="Сменить цвет ника", style=discord.ButtonStyle.secondary, custom_id="profile_color", emoji="🎨", row=2)
    async def color_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_economy()
        inv = data.get(str(interaction.user.id), {}).get("items", [])
        has_color = "цвет_ника" in inv or "vip_спермик" in inv
        if not has_color:
            for r in interaction.user.roles:
                if r.name.lower() in ["цветной", "vip спермик"]:
                    has_color = True
                    break
        if not has_color:
            await interaction.response.send_message("❌ Сначала купи `цвет_ника` в #магазин за 250 💦", ephemeral=True)
            return
        view = ColorView()
        embed = discord.Embed(title="🎨 Смена цвета ника", description="Выбери цвет для ника", color=discord.Color.from_rgb(255, 107, 139))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class ColorView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Розовый", style=discord.ButtonStyle.secondary, custom_id="color_pink", emoji="💗")
    async def pink(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.set_color(interaction, discord.Color.from_rgb(255, 107, 139))
    @discord.ui.button(label="Синий", style=discord.ButtonStyle.secondary, custom_id="color_blue", emoji="💙")
    async def blue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.set_color(interaction, discord.Color.blue())
    @discord.ui.button(label="Зеленый", style=discord.ButtonStyle.secondary, custom_id="color_green", emoji="💚")
    async def green(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.set_color(interaction, discord.Color.green())
    @discord.ui.button(label="Желтый", style=discord.ButtonStyle.secondary, custom_id="color_yellow", emoji="💛")
    async def yellow(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.set_color(interaction, discord.Color.gold())
    @discord.ui.button(label="Фиолетовый", style=discord.ButtonStyle.secondary, custom_id="color_purple", emoji="💜")
    async def purple(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.set_color(interaction, discord.Color.purple())
    async def set_color(self, interaction: discord.Interaction, color: discord.Color):
        guild = interaction.guild
        role_name = f"Цвет-{interaction.user.name}"
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            try:
                role = await guild.create_role(name=role_name, colour=color, reason="Смена цвета ника")
                try:
                    await role.edit(position=guild.me.top_role.position - 1)
                except:
                    pass
            except Exception as e:
                await interaction.response.send_message(f"❌ Ошибка создания роли: {e}", ephemeral=True)
                return
        else:
            try:
                await role.edit(colour=color)
            except Exception as e:
                await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)
                return
        try:
            if role not in interaction.user.roles:
                await interaction.user.add_roles(role)
            await interaction.response.send_message(f"✅ Цвет ника изменен! Роль {role.mention}", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Нет прав выдать роль", ephemeral=True)

class ShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Товары за спермики", style=discord.ButtonStyle.secondary, custom_id="shop_spermi", emoji="💦")
    async def spermi(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="🛒 Товары за спермики", description="Трать спермики, фарми дальше", color=discord.Color.from_rgb(255, 107, 139))
        for k, v in SHOP_ITEMS.items():
            embed.add_field(name=f"{k} — {v['price']} 💦", value=v['desc'], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, view=ShopBuyView(list(SHOP_ITEMS.keys())))

class ShopBuyView(discord.ui.View):
    def __init__(self, items=None):
        super().__init__(timeout=None)
        keys = items if items is not None else list(SHOP_ITEMS.keys())[:5]
        for key in keys[:5]:
            if key in SHOP_ITEMS:
                self.add_item(ShopBuyButton(key))

class ShopBuyButton(discord.ui.Button):
    def __init__(self, key: str):
        super().__init__(label=f"Купить {key}", style=discord.ButtonStyle.success, custom_id=f"buy_{key}")
        self.key = key
    async def callback(self, interaction: discord.Interaction):
        item = SHOP_ITEMS.get(self.key)
        if not item:
            await interaction.response.send_message("Нет такого товара", ephemeral=True)
            return
        if get_balance(interaction.user.id) < item["price"]:
            await interaction.response.send_message(f"❌ Нужно {item['price']} 💦, у тебя {get_balance(interaction.user.id)}", ephemeral=True)
            return
        add_spermi(interaction.user.id, -item["price"])
        data = load_economy()
        uid = str(interaction.user.id)
        if "items" not in data.get(uid, {}):
            data[uid]["items"] = []
        data[uid]["items"].append(self.key)
        save_economy(data)
        if item["role"]:
            role = discord.utils.get(interaction.guild.roles, name=item["role"])
            if not role:
                try:
                    role = await interaction.guild.create_role(name=item["role"])
                except:
                    pass
            if role:
                try:
                    await interaction.user.add_roles(role)
                except:
                    pass
                await interaction.response.send_message(f"✅ Куплено {self.key} за {item['price']} 💦! Роль {role.mention} выдана. Баланс: {get_balance(interaction.user.id)}", ephemeral=True)
                return
        await interaction.response.send_message(f"✅ Куплено {self.key}! Баланс: {get_balance(interaction.user.id)}", ephemeral=True)

# ============ EVENTS ============
@bot.event
async def on_ready():
    print(f"Бот {bot.user} запущен! На {len(bot.guilds)} серверах")
    bot.add_view(VerifyView())
    bot.add_view(ProfileView())
    bot.add_view(ColorView())
    bot.add_view(ShopView())
    bot.add_view(ShopBuyView())
    try:
        ensure_db()
    except:
        pass
    try:
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced_guild = await bot.tree.sync(guild=guild)
            print(f"Синхронизировано для гильдии {config.GUILD_ID}: {len(synced_guild)} команд")
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            print("Глобальные очищены (1 копия, без дублей)")
        else:
            synced = await bot.tree.sync()
            print(f"Синхронизировано глобально {len(synced)} команд")
    except Exception as e:
        print(f"Ошибка синхронизации: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    if config.WELCOME_CHANNEL_ID:
        ch = bot.get_channel(config.WELCOME_CHANNEL_ID)
        if ch:
            guild = member.guild
            # Красивый welcome
            embed = discord.Embed(
                title=f"❄️ Добро пожаловать в {guild.name}! ❄️",
                description=(
                    f"Привет, {member.mention}!\n\n"
                    f"Ты — **{guild.member_count}**-й участник нашего дворца!\n"
                    f"✨ Пройди **Бусификацию** в <#{discord.utils.get(guild.text_channels, name='🚐・бусификация').id if discord.utils.get(guild.text_channels, name='🚐・бусификация') else 0}>\n"
                    f"📜 Прочитай правила в <#{discord.utils.get(guild.text_channels, name='rules').id if discord.utils.get(guild.text_channels, name='rules') else (discord.utils.get(guild.text_channels, name='📜・rules').id if discord.utils.get(guild.text_channels, name='📜・rules') else 0)}>\n"
                    f"🪪 Открой профиль в <#{discord.utils.get(guild.text_channels, name='🪪・профиль').id if discord.utils.get(guild.text_channels, name='🪪・профиль') else 0}> и забирай ежедневку!\n\n"
                    f"*Рады видеть тебя, не будь душным и фарми спермики!* 💦"
                ),
                color=discord.Color.from_rgb(88, 101, 242)
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            if guild.icon:
                embed.set_author(name=guild.name, icon_url=guild.icon.url)
            embed.set_image(url="https://i.imgur.com/8Km9tLL.png")
            embed.set_footer(text=f"ID: {member.id} • Зашел: {discord.utils.format_dt(member.joined_at, style='R') if member.joined_at else ''}", icon_url=member.display_avatar.url)
            embed.add_field(name="🎮 Онлайн", value=f"{len([m for m in guild.members if m.status != discord.Status.offline])} в сети", inline=True)
            embed.add_field(name="📦 Бустов", value=f"{guild.premium_subscription_count} бустов", inline=True)
            try:
                await ch.send(content=f"{member.mention}", embed=embed)
            except:
                await ch.send(embed=embed)
    if config.LOG_CHANNEL_ID:
        ch = bot.get_channel(config.LOG_CHANNEL_ID)
        if ch:
            await ch.send(f"📥 Зашел {member.mention} `{member.id}`")

@bot.event
async def on_member_remove(member: discord.Member):
    if config.LOG_CHANNEL_ID:
        ch = bot.get_channel(config.LOG_CHANNEL_ID)
        if ch:
            await ch.send(f"📤 Вышел {member.display_name} `{member.id}`")

@bot.event
async def on_message(message: discord.Message):
    # Сначала обрабатываем команды с префиксом "!"
    await bot.process_commands(message)

    # Музыка: сначала проверяем "Анечка зайди/включи" — не отдаём в AI
    try:
        if await _handle_music_triggers(message):
            return
    except Exception as e:
        print(f"music trigger error: {e}")

    # AI: отвечает на пинг или имя
    triggered, prompt = _is_ai_triggered(message)
    if not triggered:
        return

    # Кулдаун анти-спам
    now = time.time()
    last = _ai_cooldown.get(message.author.id, 0)
    if now - last < _cfg("AI_COOLDOWN", 5):
        # молча игнорим или можно реагировать
        return
    _ai_cooldown[message.author.id] = now

    if not prompt or len(prompt.strip()) < 1:
        await message.reply("👋 Да, я тут! Напиши вопрос после моего имени или пинга. Напр: `@бот как дела?` или `мила расскажи анекдот`")
        return

    # Резолвим пинги внутри промпта: <@123> -> @Имя, чтобы понимала про кого речь (не путать автора и упомянутого)
    resolved_prompt = prompt
    for u in message.mentions:
        if u.id != bot.user.id:
            resolved_prompt = resolved_prompt.replace(f"<@{u.id}>", f"@{u.display_name}").replace(f"<@!{u.id}>", f"@{u.display_name}")
    # также резолвим роли <@&id>
    if message.guild:
        for r in message.role_mentions:
            resolved_prompt = resolved_prompt.replace(f"<@&{r.id}>", f"@{r.name}")
    if len(resolved_prompt) > 1500:
        resolved_prompt = resolved_prompt[:1500]
    # Защита от слишком длинных промптов
    if len(prompt) > 1500:
        prompt = prompt[:1500]

    async with message.channel.typing():
        # небольшая задержка чтобы выглядело живее, + даем время набрать историю
        answer = await _ask_ai(resolved_prompt, message.channel.id, message.author.display_name, message.author.id, message.guild.name if message.guild else "")

    try:
        await message.reply(answer, mention_author=False, allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False))
    except discord.HTTPException:
        try:
            await message.channel.send(f"{message.author.mention} {answer}", allowed_mentions=discord.AllowedMentions(users=True))
        except:
            pass

    # Команда очистки истории AI (только сам пользователь или админ) — по слову "забудь"
    # не делаем отдельной команды, можно расширить позже

# ============ SLASH COMMANDS ============
@bot.tree.command(name="верификация", description="Создать сообщение для верификации (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_verify(interaction: discord.Interaction, канал: discord.TextChannel = None):
    ch = канал or interaction.channel
    embed = discord.Embed(title="🚐 БУСИФИКАЦИЯ", description="**Добро пожаловать в бусик!**\n\nНажми на кнопку ниже, чтобы пройти бусификацию и получить доступ к серверу!\n\n> ⚠️ Уклонение от бусификации карается ТЦК\n> ✅ После нажатия откроются все каналы.", color=discord.Color.dark_gold())
    embed.set_image(url="https://i.imgflip.com/6e0a5u.jpg")
    embed.set_footer(text=f"{interaction.guild.name} • Бусификация на связи")
    await ch.send(embed=embed, view=VerifyView())
    await interaction.response.send_message(f"✅ Панель Бусификации создана в {ch.mention}", ephemeral=True)

@bot.tree.command(name="основа", description="Создать канал бусификация с верификацией (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_base(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    role = discord.utils.get(guild.roles, name="бусифицированный")
    if not role:
        try:
            role = await guild.create_role(name="бусифицированный", colour=discord.Colour.gold(), reason="Роль для Бусификации /основа")
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав создавать роль `бусифицированный`.", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка создания роли: {e}", ephemeral=True)
            return
    category = discord.utils.get(guild.categories, name="🚐・БУСИФИКАЦИЯ") or discord.utils.get(guild.categories, name="БУСИФИКАЦИЯ")
    if not category:
        cat_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
            role: discord.PermissionOverwrite(view_channel=False)
        }
        try:
            category = await guild.create_category("🚐・БУСИФИКАЦИЯ", overwrites=cat_overwrites, reason="Категория для Бусификации /основа")
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав создавать категории.", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка создания категории: {e}", ephemeral=True)
            return
    else:
        try:
            await category.set_permissions(guild.default_role, view_channel=True)
            await category.set_permissions(role, view_channel=False)
        except:
            pass
    channel = discord.utils.get(guild.text_channels, name="🚐・бусификация") or discord.utils.get(guild.text_channels, name="бусификация")
    already_had_channel = channel is not None
    has_panel = False
    if channel:
        try:
            async for msg in channel.history(limit=5):
                if msg.author == guild.me and msg.embeds and msg.embeds[0].title and "БУСИФИКАЦИЯ" in msg.embeds[0].title:
                    has_panel = True
                    break
        except:
            pass
        if channel.category != category:
            try:
                await channel.edit(category=category)
            except:
                pass
    if not channel:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            role: discord.PermissionOverwrite(view_channel=False)
        }
        try:
            channel = await guild.create_text_channel("🚐・бусификация", category=category, overwrites=overwrites, topic="Пройди бусификацию чтобы получить доступ", reason="Команда /основа")
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав создавать каналы.", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка создания канала: {e}", ephemeral=True)
            return
    hidden_count = 0
    for ch in guild.channels:
        if ch.id == channel.id or ch.id == category.id:
            try:
                await ch.set_permissions(guild.default_role, view_channel=True, send_messages=False if isinstance(ch, discord.TextChannel) else None, read_message_history=True if isinstance(ch, discord.TextChannel) else None)
                await ch.set_permissions(role, view_channel=False)
            except:
                pass
            continue
        try:
            await ch.set_permissions(guild.default_role, view_channel=False)
            await ch.set_permissions(role, view_channel=True)
            hidden_count += 1
        except:
            continue
    embed = discord.Embed(title="🚐 БУСИФИКАЦИЯ", description="**Вас остановили ТЦК!**\n\nЧтобы избежать поездки в бусике — пройди бусификацию 👇\n\nНажми **Пройти Бусификацию** и получи доступ к серверу.\n\n> 🫡 *Локальный мем сервера — бусификация обязательна*\n> Без роли `бусифицированный` ты не увидишь другие каналы!", color=discord.Color.gold())
    embed.set_footer(text=f"{guild.name} • Не сопротивляйся бусификации")
    if not has_panel:
        try:
            async for msg in channel.history(limit=10):
                if msg.author == guild.me and msg.embeds:
                    await msg.delete()
        except:
            pass
        await channel.send(embed=embed, view=VerifyView())
    if already_had_channel and has_panel:
        await interaction.followup.send(f"🔄 Перенастроено! Роль {role.mention} + категория {category.name} + канал {channel.mention}\n🔒 Скрыто/обновлено {hidden_count} каналов.", ephemeral=True)
    else:
        await interaction.followup.send(f"✅ Готово! Категория {category.name} + канал {channel.mention} + роль {role.mention}\n🔒 Скрыто {hidden_count} каналов.", ephemeral=True)

@bot.tree.command(name="роли", description="Создать панель с выдачей ролей (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_roles(interaction: discord.Interaction, канал: discord.TextChannel = None):
    if not config.SELF_ROLES:
        await interaction.response.send_message("❌ В .env не настроены SELF_ROLES.", ephemeral=True)
        return
    ch = канал or interaction.channel
    guild = interaction.guild
    embed = discord.Embed(title="🎭 Выбор ролей", description="Нажми на кнопку роли, чтобы получить или снять её:\n\n" + "\n".join([f"• <@&{rid}>" for rid in config.SELF_ROLES]), color=discord.Color.gold())
    view = RolesButtonView(guild)
    await ch.send(embed=embed, view=view)
    await interaction.response.send_message(f"✅ Панель ролей создана в {ch.mention}", ephemeral=True)

@bot.tree.command(name="меню", description="Создать каналы профиль и магазин как у Милки (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_menu(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    role = get_verify_role(guild) or discord.utils.get(guild.roles, name="бусифицированный")
    category = discord.utils.get(guild.categories, name="Меню")
    if not category:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True)
            }
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True)
            category = await guild.create_category("Меню", overwrites=overwrites, reason="/меню")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка категории: {e}", ephemeral=True)
            return
    profil_ch = discord.utils.get(guild.text_channels, name="профиль") or discord.utils.get(guild.text_channels, name="🪪・профиль")
    if not profil_ch:
        try:
            profil_ch = await guild.create_text_channel("🪪・профиль", category=category, topic="Профиль, инвентарь и роли", reason="/меню")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка канал профиль: {e}", ephemeral=True)
            return
    shop_ch = discord.utils.get(guild.text_channels, name="магазин") or discord.utils.get(guild.text_channels, name="🛒・магазин")
    if not shop_ch:
        try:
            shop_ch = await guild.create_text_channel("🛒・магазин", category=category, topic="Магазин за спермики", reason="/меню")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка канал магазин: {e}", ephemeral=True)
            return
    for ch in [profil_ch, shop_ch]:
        try:
            async for msg in ch.history(limit=20):
                if msg.author == guild.me and msg.embeds:
                    await msg.delete()
        except:
            pass
    embed_banner_prof = discord.Embed(color=discord.Color.from_rgb(255, 107, 139))
    embed_banner_prof.set_image(url="https://yt3.ggpht.com/t2oynaaQq3aVvMuzymoqvK6m8VGPu1mV5Krr4x9YRvw0bHEKv4mwXteK3DmTqLo4j2US8OW0b21y4A=s416-c-fcrop64=1,380b0000c7f4ffff-nd-v1-rwa")
    await profil_ch.send(embed=embed_banner_prof)
    embed_prof = discord.Embed(title="Профиль • Инвентарь и Роли", description="Управляй ролями, инвентарем и спермиками", color=discord.Color.from_rgb(255, 107, 139))
    embed_prof.add_field(name="📦 Инвентарь", value="Предметы — как пульт ролями. Чтобы получить роль, купи в магазине.", inline=True)
    embed_prof.add_field(name="🎨 Фоны", value="Крути гачу за спермики и открывай фоны профиля", inline=True)
    embed_prof.add_field(name="🎭 Роли", value="Цветные роли выделят тебя в чате!", inline=True)
    embed_prof.set_footer(text="спермики • профиль")
    bot.add_view(ProfileView())
    await profil_ch.send(embed=embed_prof, view=ProfileView())
    embed_prof2 = discord.Embed(color=discord.Color.from_rgb(255, 107, 139))
    embed_prof2.description = f"Все твои предметы: кейсы, роли. Покупай в {shop_ch.mention}\nБаланс: /баланс | Ежедневка: /ежедневка"
    await profil_ch.send(embed=embed_prof2)
    embed_banner = discord.Embed(title="МАГАЗИНЫ — Фармим, закупаемся!", color=discord.Color.from_rgb(255, 107, 139))
    embed_banner.set_image(url="https://img.magnific.com/premium-photo/cute-anime-girl-hoodie-wallpaper_776894-105948.jpg?semt=ais_hybrid")
    await shop_ch.send(embed=embed_banner)
    embed_shop = discord.Embed(title="Валюта Сервера", color=discord.Color.from_rgb(255, 107, 139))
    embed_shop.description = "**Фармим, закупаемся!**"
    embed_shop.add_field(name="💦 Спермики", value="Получаются за активность: сообщения, войс, ивенты\nКнопки в #профиль: `Ежедневка +100`, `Перевести`, `Инвентарь`, `Рулетка дроноеба`", inline=False)
    embed_shop.set_footer(text="Black ICE Palace • спермики")
    bot.add_view(ShopView())
    bot.add_view(ShopBuyView())
    await shop_ch.send(embed=embed_shop, view=ShopView())
    if role:
        try:
            await category.set_permissions(guild.default_role, view_channel=False)
            await category.set_permissions(role, view_channel=True)
        except:
            pass
    await interaction.followup.send(f"✅ Меню создано: {category.name} → {profil_ch.mention} + {shop_ch.mention}.", ephemeral=True)

@bot.tree.command(name="правила", description="Опубликовать правила Black ICE Palace в #rules (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def publish_rules(interaction: discord.Interaction, канал: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    ch = канал or discord.utils.get(guild.text_channels, name="rules") or discord.utils.get(guild.text_channels, name="📜・rules") or interaction.channel
    embed = discord.Embed(title="📜 ПРАВИЛА BLACK ICE PALACE 2026 edition 📜", description="**Читать обязательно, иначе получишь пизды от жизни**\n\u200b", color=discord.Color.from_rgb(255, 107, 139))
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
    rules = (
        "1️⃣ 💗 **Маты можно**, мы не в детском саду. Пизди как хочешь, но если ты через каждое слово хуяришь \"бля\" просто чтобы казаться крутым - ты долбаеб\n\n"
        "2️⃣ 💗 **Чернуха и рофлы разрешены.** Шути про что хочешь, но если твой рофл уровня \"мамка умерла хаха\" - то ты не смешной, ты просто еблан.\n\n"
        "3️⃣ 💗 **Мемы можно, но не засирай чат** 30-ю мемами подряд. Кинул 1-2 смешных - красава. Кинул 20 - ты заебал всех и идешь в мут\n\n"
        "4️⃣ 💗 **В войсе микро настрой.** Если ты хрипишь как дед после 40 лет курения - включи шумодав или сиди в муте\n\n"
        "5️⃣ 💗 **Орать как ебанутый в войсе - нельзя.** Хочешь поорать - иди к мамке на кухню и ори что жрать нечего.\n\n"
        "6️⃣ 💗 **Никнейм** \"ххнагибатор1488ххх\" - сразу смена ника. Поставь что-то нормальное, а не хуйню из 2012 года.\n\n"
        "7️⃣ 💗 **Рекламу левых серверов, тг, скам-ссылок кидать - нельзя.** Кинул - бан нахуй без предупреждения\n\n"
        "8️⃣ 💗 **Порнуху, расчленку, жесть с телеги кидать - нельзя.** Хочешь дрочить - дрочи в лс, а не в общий чат\n\n"
        "9️⃣ 💗 **Зашел в войс и сидишь молча 3 часа как призрак ебаный - странно.** Хоть \"ку\" скажи, а то думаем ты умер.\n\n"
        "🔟 💗 **Ливать и заходить по 100 раз за минуту - не надо.** Дискорд и так лагает, а ты еще и мигаешь как гирлянда.\n\n"
        "1️⃣1️⃣ 💗 **Подъебать друга - святое.** Травить толпой одного человека - ты чмо. Чувствуй разницу, ебанат.\n\n"
        "1️⃣2️⃣ 💗 **Сраться из-за игр - ок, бывает.** Но если вы уже 2 часа сретесь кто кому мать ебал в валоранте - идите в лс и ебите друг друга там.\n\n"
        "1️⃣3️⃣ 💗 **Ботов не ломай и не спамь командами в болталке.** Для этого есть канал #🤖・команды. Ты же не срешь на кухне, а в туалете.\n\n"
        "1️⃣4️⃣ 💗 **Не будь душным.** Если все ржут, а ты начал лекцию на тему \"почему это не смешно и аморально\" - тебя просто заигнорят, душнила.\n\n"
        "1️⃣5️⃣ 💗 **Приглашать своих друзей можно, но если ты притащил какого-то еблана который начал всех оскорблять - вы улетите вместе паровозиком**\n\n"
        "1️⃣6️⃣ 💗 **Ушел на месяц - похуй.** Вернешься - примем. Только не надо каждый день писать \"я ливаю навсегда\" и возвращаться через час, клоун.\n\n"
        "1️⃣7️⃣ 💗 **АФК в войсе - ок, но не занимай игровой войс.** Хочешь поспать - иди в 💤・АФК, а не в 🎮・Игры.\n\n"
        "1️⃣8️⃣ 💗 **Самоубийством, селфхармом не шутим.** Чернуха про смерть - ок, но если чел пишет что ему хуево - не надо писать \"ну так убейся\", ты не смешной, ты мразь.\n\n"
        "1️⃣9️⃣ 💗 **Админы это святые, выше них только бог, и то не факт**\n\n"
        "2️⃣0️⃣ 💗 **Если админ сказал - значит так и будет.** Сказал заткнуться - затыкаемся. Сказал в мут - идешь в мут. Спорить с админом - идея уровня пойти пиздить медведя голыми руками."
    )
    embed.description += rules
    embed.add_field(name="⚖️ Наказания:", value="⚠️ 1 раз - предупреждение и подзатыльник\n🔇 2 раз - мут на 12 часов\n🔨 3 раз - бан на 3 дня\n💀 Дальше - пермач нахуй 👋", inline=False)
    embed.add_field(name="💸 Пожертвования для бустов -", value="Сканируй QR ниже", inline=False)
    embed.set_footer(text="Black ICE Palace • 2026 • Читать обязательно")
    try:
        await ch.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Не смог отправить в {ch.mention}: {e}", ephemeral=True)
        return
    qr_embed = discord.Embed(title="Пожертвования", description="**Уруев Дмитрий Денисович**\nНомер договора `5664748331`", color=discord.Color.from_rgb(255, 107, 139))
    qr_embed.set_image(url="https://api.qrserver.com/v1/create-qr-code/?size=500x500&data=5664748331")
    qr_embed.set_footer(text="Отсканируй для доната")
    await ch.send(embed=qr_embed)
    ps_embed = discord.Embed(description="*P. S. Все пожертвования на сервер носят добровольно-принудительный характер, сказали - делай* 😉", color=discord.Color.from_rgb(255, 107, 139))
    await ch.send(embed=ps_embed)
    await interaction.followup.send(f"✅ Правила опубликованы в {ch.mention}", ephemeral=True)

@bot.tree.command(name="велкомтест", description="Тест приветствия (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def welcometest(interaction: discord.Interaction):
    guild = interaction.guild
    ch = bot.get_channel(config.WELCOME_CHANNEL_ID) if config.WELCOME_CHANNEL_ID else interaction.channel
    if not ch:
        ch = interaction.channel
    member = interaction.user
    embed = discord.Embed(
        title=f"❄️ Добро пожаловать в {guild.name}! ❄️ (ТЕСТ)",
        description=(
            f"Привет, {member.mention}!\n\n"
            f"Ты — **{guild.member_count}**-й участник нашего дворца!\n"
            f"✨ Пройди **Бусификацию** в <#{discord.utils.get(guild.text_channels, name='🚐・бусификация').id if discord.utils.get(guild.text_channels, name='🚐・бусификация') else 0}>\n"
            f"📜 Прочитай правила в <#{discord.utils.get(guild.text_channels, name='rules').id if discord.utils.get(guild.text_channels, name='rules') else 0}>\n"
            f"🪪 Открой профиль в <#{discord.utils.get(guild.text_channels, name='🪪・профиль').id if discord.utils.get(guild.text_channels, name='🪪・профиль') else 0}> и забирай ежедневку!\n\n"
            f"*Рады видеть тебя, не будь душным и фарми спермики!* 💦"
        ),
        color=discord.Color.from_rgb(88, 101, 242)
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    if guild.icon:
        embed.set_author(name=guild.name, icon_url=guild.icon.url)
    embed.set_image(url="https://i.imgur.com/8Km9tLL.png")
    embed.set_footer(text=f"ID: {member.id} • Тест", icon_url=member.display_avatar.url)
    await ch.send(content=f"{member.mention}", embed=embed)
    await interaction.response.send_message(f"✅ Тест отправлен в {ch.mention}", ephemeral=True)

@bot.tree.command(name="очистка", description="Удалить сообщения (только модер)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, количество: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=количество)
    await interaction.followup.send(f"🧹 Удалено {len(deleted)} сообщений", ephemeral=True)
    if config.LOG_CHANNEL_ID:
        ch = bot.get_channel(config.LOG_CHANNEL_ID)
        if ch:
            await ch.send(f"🧹 {interaction.user.mention} очистил {len(deleted)} сообщений в {interaction.channel.mention}")

@bot.tree.command(name="кик", description="Кикнуть участника")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, участник: discord.Member, причина: str = "Не указана"):
    try:
        await участник.kick(reason=причина)
        await interaction.response.send_message(f"👢 {участник.mention} кикнут. Причина: {причина}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Нет прав кикать этого участника (роль бота ниже).", ephemeral=True)

@bot.tree.command(name="бан", description="Забанить участника")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, участник: discord.Member, причина: str = "Не указана"):
    try:
        await участник.ban(reason=причина)
        await interaction.response.send_message(f"🔨 {участник.mention} забанен. Причина: {причина}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Нет прав банить.", ephemeral=True)

@bot.tree.command(name="мут", description="Замутить на время")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, участник: discord.Member, минуты: app_commands.Range[int, 1, 10080], причина: str = "Не указана"):
    try:
        await участник.timeout(timedelta(minutes=минуты), reason=причина)
        await interaction.response.send_message(f"🔇 {участник.mention} замучен на {минуты} мин. Причина: {причина}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Нет прав мутить.", ephemeral=True)

@bot.tree.command(name="юзер", description="Инфо о пользователе")
async def userinfo(interaction: discord.Interaction, участник: discord.Member = None):
    user = участник or interaction.user
    embed = discord.Embed(title=f"Инфо — {user.display_name}", color=user.color)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="ID", value=user.id)
    embed.add_field(name="Аккаунт создан", value=discord.utils.format_dt(user.created_at, style="F"))
    if isinstance(user, discord.Member):
        embed.add_field(name="Зашел на сервер", value=discord.utils.format_dt(user.joined_at, style="F") if user.joined_at else "?" )
        embed.add_field(name="Роли", value=", ".join([r.mention for r in user.roles[1:]]) or "Нет ролей", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="сервер", description="Инфо о сервере")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=g.name, description=g.description, color=discord.Color.blurple())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Участников", value=g.member_count)
    embed.add_field(name="Создан", value=discord.utils.format_dt(g.created_at, style="D"))
    embed.add_field(name="Каналов", value=len(g.channels))
    embed.add_field(name="Ролей", value=len(g.roles))
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="баланс", description="Показать баланс спермиков")
async def balance(interaction: discord.Interaction, пользователь: discord.Member = None):
    user = пользователь or interaction.user
    bal = get_balance(user.id)
    embed = discord.Embed(title="💦 Баланс спермиков", description=f"{user.mention} имеет **{bal}** спермиков", color=discord.Color.blue())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ежедневка", description="Получить ежедневные спермики")
async def daily(interaction: discord.Interaction):
    data = load_economy()
    uid = str(interaction.user.id)
    now = time.time()
    last = data.get(uid, {}).get("daily", 0)
    if now - last < 86400:
        left = 86400 - (now - last)
        h = int(left // 3600)
        m = int((left % 3600) // 60)
        await interaction.response.send_message(f"⏳ Уже брал! Через {h}ч {m}м", ephemeral=True)
        return
    bal = add_spermi(interaction.user.id, 100)
    data = load_economy()
    data[uid]["daily"] = now
    save_economy(data)
    await interaction.response.send_message(f"✅ Получено 100 спермиков! Баланс: {bal}")

@bot.tree.command(name="топ-спермиков", description="Топ по спермикам")
async def top_spermi(interaction: discord.Interaction):
    data = load_economy()
    top = sorted(data.items(), key=lambda x: x[1].get("balance", 0), reverse=True)[:10]
    if not top:
        await interaction.response.send_message("Пока пусто", ephemeral=True)
        return
    desc = ""
    for i, (uid, info) in enumerate(top, 1):
        member = interaction.guild.get_member(int(uid))
        name = member.display_name if member else f"ID {uid}"
        desc += f"{i}. {name} — {info.get('balance',0)} 💦\n"
    embed = discord.Embed(title="🏆 Топ спермиков", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

# ============ ПРОМОКОДЫ И БАЛАНС (админ в ЛС) ============
def is_admin_check(interaction: discord.Interaction):
    if interaction.guild and interaction.user.guild_permissions.administrator:
        return True
    if config.GUILD_ID:
        g = bot.get_guild(config.GUILD_ID)
        if g:
            m = g.get_member(interaction.user.id)
            if m and m.guild_permissions.administrator:
                return True
    return False

class PromoModal(discord.ui.Modal, title="Ввести промокод"):
    code = discord.ui.TextInput(label="Промокод", placeholder="SUPER2026", required=True, max_length=20)
    async def on_submit(self, interaction: discord.Interaction):
        code = self.code.value.strip().upper()
        conn = get_db()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT reward, max_uses, uses FROM promo_codes WHERE code=%s", (code,))
                row = cur.fetchone()
                if not row:
                    await interaction.response.send_message("❌ Неверный промокод", ephemeral=True)
                    cur.close(); conn.close()
                    return
                reward, max_uses, uses = row
                cur.execute("SELECT 1 FROM promo_uses WHERE code=%s AND user_id=%s", (code, str(interaction.user.id)))
                if cur.fetchone():
                    await interaction.response.send_message("❌ Ты уже использовал этот промокод", ephemeral=True)
                    cur.close(); conn.close()
                    return
                if uses >= max_uses:
                    await interaction.response.send_message("❌ Промокод закончился", ephemeral=True)
                    cur.close(); conn.close()
                    return
                cur.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=%s", (code,))
                cur.execute("INSERT INTO promo_uses (code, user_id) VALUES (%s,%s)", (code, str(interaction.user.id)))
                cur.execute("INSERT INTO economy (user_id, balance) VALUES (%s,0) ON CONFLICT (user_id) DO NOTHING", (str(interaction.user.id),))
                cur.execute("UPDATE economy SET balance = balance + %s WHERE user_id=%s", (reward, str(interaction.user.id)))
                cur.close(); conn.close()
                await interaction.response.send_message(f"✅ Промокод активирован! +{reward} 💦", ephemeral=True)
                return
            except Exception as e:
                try:
                    conn.close()
                except:
                    pass
                await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)
                return
        await interaction.response.send_message("❌ База промокодов не настроена (нет Postgres)", ephemeral=True)

@bot.tree.command(name="создать-промокод", description="Создать промокод (только админ, можно в ЛС)")
async def create_promo(interaction: discord.Interaction, код: str, награда: app_commands.Range[int, 1, 10000], лимит: app_commands.Range[int, 1, 1000] = 1):
    if not is_admin_check(interaction):
        await interaction.response.send_message("❌ Только админ", ephemeral=True)
        return
    code = код.strip().upper()
    conn = get_db()
    if not conn:
        await interaction.response.send_message("❌ Нет БД", ephemeral=True)
        return
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO promo_codes (code, reward, max_uses, uses, created_by, created_at) VALUES (%s,%s,%s,0,%s,%s) ON CONFLICT (code) DO NOTHING", (code, награда, лимит, str(interaction.user.id), int(time.time())))
        if cur.rowcount == 0:
            await interaction.response.send_message("❌ Такой промокод уже есть", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Промокод `{code}` создан: +{награда} 💦, лимит {лимит}", ephemeral=True)
        cur.close(); conn.close()
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

@bot.tree.command(name="редактировать-промокод", description="Редактировать промокод (админ, ЛС)")
async def edit_promo(interaction: discord.Interaction, код: str, награда: app_commands.Range[int, 1, 10000] = None, лимит: app_commands.Range[int, 1, 1000] = None):
    if not is_admin_check(interaction):
        await interaction.response.send_message("❌ Только админ", ephemeral=True)
        return
    code = код.strip().upper()
    conn = get_db()
    if not conn:
        await interaction.response.send_message("❌ Нет БД", ephemeral=True)
        return
    try:
        cur = conn.cursor()
        if награда is not None:
            cur.execute("UPDATE promo_codes SET reward=%s WHERE code=%s", (награда, code))
        if лимит is not None:
            cur.execute("UPDATE promo_codes SET max_uses=%s WHERE code=%s", (лимит, code))
        cur.close(); conn.close()
        await interaction.response.send_message(f"✅ Промокод `{code}` обновлен", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

@bot.tree.command(name="удалить-промокод", description="Удалить промокод (админ, ЛС)")
async def delete_promo(interaction: discord.Interaction, код: str):
    if not is_admin_check(interaction):
        await interaction.response.send_message("❌ Только админ", ephemeral=True)
        return
    code = код.strip().upper()
    conn = get_db()
    if not conn:
        await interaction.response.send_message("❌ Нет БД", ephemeral=True)
        return
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM promo_codes WHERE code=%s", (code,))
        cur.execute("DELETE FROM promo_uses WHERE code=%s", (code,))
        cur.close(); conn.close()
        await interaction.response.send_message(f"✅ Промокод `{code}` удален", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

@bot.tree.command(name="список-промокодов", description="Список промокодов (админ, ЛС)")
async def list_promo(interaction: discord.Interaction):
    if not is_admin_check(interaction):
        await interaction.response.send_message("❌ Только админ", ephemeral=True)
        return
    conn = get_db()
    if not conn:
        await interaction.response.send_message("❌ Нет БД", ephemeral=True)
        return
    try:
        cur = conn.cursor()
        cur.execute("SELECT code, reward, max_uses, uses FROM promo_codes")
        rows = cur.fetchall()
        cur.close(); conn.close()
        if not rows:
            await interaction.response.send_message("Промокодов нет", ephemeral=True)
            return
        desc = "\n".join([f"`{r[0]}` — +{r[1]} 💦 {r[3]}/{r[2]}" for r in rows])
        embed = discord.Embed(title="Промокоды", description=desc, color=discord.Color.gold())
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

@bot.tree.command(name="редактировать-баланс", description="Выдать/забрать спермики (админ, можно в ЛС)")
async def edit_balance(interaction: discord.Interaction, пользователь: str, количество: int):
    if not is_admin_check(interaction):
        await interaction.response.send_message("❌ Только админ", ephemeral=True)
        return
    import re
    m = re.search(r"\d{15,}", пользователь)
    if not m:
        await interaction.response.send_message("❌ Укажи ID или @упоминание", ephemeral=True)
        return
    uid = int(m.group(0))
    new_bal = add_spermi(uid, количество)
    guild = bot.get_guild(config.GUILD_ID) if config.GUILD_ID else None
    member = guild.get_member(uid) if guild else None
    name = member.mention if member else f"<@{uid}>"
    sign = "+" if количество > 0 else ""
    await interaction.response.send_message(f"✅ Баланс {name} изменен на {sign}{количество} 💦 → теперь {new_bal}", ephemeral=True)

@bot.tree.command(name="промокод", description="Активировать промокод")
async def redeem_promo(interaction: discord.Interaction, код: str):
    code = код.strip().upper()
    conn = get_db()
    if not conn:
        await interaction.response.send_message("❌ Нет БД", ephemeral=True)
        return
    try:
        cur = conn.cursor()
        cur.execute("SELECT reward, max_uses, uses FROM promo_codes WHERE code=%s", (code,))
        row = cur.fetchone()
        if not row:
            await interaction.response.send_message("❌ Неверный промокод", ephemeral=True)
            cur.close(); conn.close()
            return
        reward, max_uses, uses = row
        cur.execute("SELECT 1 FROM promo_uses WHERE code=%s AND user_id=%s", (code, str(interaction.user.id)))
        if cur.fetchone():
            await interaction.response.send_message("❌ Ты уже использовал", ephemeral=True)
            cur.close(); conn.close()
            return
        if uses >= max_uses:
            await interaction.response.send_message("❌ Закончился", ephemeral=True)
            cur.close(); conn.close()
            return
        cur.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=%s", (code,))
        cur.execute("INSERT INTO promo_uses (code, user_id) VALUES (%s,%s)", (code, str(interaction.user.id)))
        cur.execute("INSERT INTO economy (user_id, balance) VALUES (%s,0) ON CONFLICT (user_id) DO NOTHING", (str(interaction.user.id),))
        cur.execute("UPDATE economy SET balance = balance + %s WHERE user_id=%s", (reward, str(interaction.user.id)))
        cur.close(); conn.close()
        await interaction.response.send_message(f"✅ +{reward} 💦 активировано!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)

# ============ AI SLASH COMMANDS ============
@bot.tree.command(name="ai", description="Спросить у AI (работает и по пингу/имени)")
@app_commands.describe(prompt="Что спросить у бота")
async def ai_chat(interaction: discord.Interaction, prompt: str):
    try:
        if not _cfg("AI_ENABLED", True):
            await interaction.response.send_message("❌ AI выключен (AI_ENABLED=false)", ephemeral=True)
            return
        if not _cfg("AI_API_KEY", ""):
            await interaction.response.send_message("❌ AI не настроен: нет OPENROUTER_API_KEY в Variables на Railway\nВозьми ключ на https://openrouter.ai/keys и добавь в Variables -> New Variable", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        gname = interaction.guild.name if interaction.guild else ""
        answer = await _ask_ai(prompt[:1500], interaction.channel_id or 0, interaction.user.display_name, interaction.user.id, gname)
        try:
            await interaction.followup.send(answer)
        except Exception as e:
            print(f"ai followup fail: {e}")
            try:
                await interaction.followup.send(answer[:1900])
            except:
                pass
    except Exception as e:
        print(f"ai_chat error: {e}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Ошибка AI: {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Ошибка AI: {e}", ephemeral=True)
        except:
            pass

@bot.tree.command(name="ai-reset", description="Сбросить историю AI в этом канале")
async def ai_reset(interaction: discord.Interaction):
    _ai_history[interaction.channel_id or 0].clear()
    await interaction.response.send_message("✅ История AI в этом канале очищена", ephemeral=True)

@bot.tree.command(name="ai-status", description="Статус AI")
async def ai_status(interaction: discord.Interaction):
    enabled = "✅ Вкл" if _cfg("AI_ENABLED", True) else "❌ Выкл"
    has_key = "✅ есть" if _cfg("AI_API_KEY", "") else "❌ нет (добавь OPENROUTER_API_KEY в Variables)"
    names = ", ".join(_cfg("AI_TRIGGER_NAMES", [])) if _cfg("AI_TRIGGER_NAMES", []) else "(только пинг + имя бота)"
    embed = discord.Embed(title="🤖 AI статус", color=discord.Color.blurple())
    embed.add_field(name="Включен", value=enabled, inline=True)
    embed.add_field(name="Ключ", value=has_key, inline=True)
    embed.add_field(name="Модель", value=f"`{_cfg('AI_MODEL','?')}`", inline=False)
    embed.add_field(name="Триггеры", value=names, inline=False)
    embed.add_field(name="Как общаться", value="• Пингани бота: `@бот привет`\n• Назови по имени: `мила как дела?`\n• Или `/ai prompt: привет`", inline=False)
    embed.set_footer(text=f"Base: {_cfg('AI_BASE_URL','?')} • Кулдаун {_cfg('AI_COOLDOWN',5)}с")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============ MUSIC SLASH COMMANDS ============
@bot.tree.command(name="join", description="Анечка зайдёт в твой войс")
async def slash_join(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("Зайди в войс сначала! 🚁", ephemeral=True)
        return
    await interaction.response.defer()
    # создаём фейк message для переиспользования логики
    class FakeMsg:
        def __init__(self, inter):
            self.author = inter.user
            self.guild = inter.guild
            self.channel = inter.channel
            self.reply = inter.followup.send
            self.content = "анечка зайди"
        async def reply(self, *a, **kw):
            return await interaction.followup.send(*a, **kw)
    # проще напрямую
    try:
        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            await vc.move_to(channel)
        else:
            await channel.connect()
        await interaction.followup.send(f"🚁 Влетела в `{channel.name}`! Пиши `Анечка включи <песня>`")
    except Exception as e:
        await interaction.followup.send(f"❌ {e}")

@bot.tree.command(name="play", description="Включить музыку (ссылка или название)")
@app_commands.describe(query="Ссылка YouTube или название песни")
async def slash_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    # переиспользуем enqueue но нужен message-like
    class Dummy:
        def __init__(self):
            self.guild = interaction.guild
            self.author = interaction.user
            self.channel = interaction.channel
            self.content = f"анечка включи {query}"
        async def reply(self, msg, **kw):
            await interaction.followup.send(msg, **kw)
    dummy = Dummy()
    # копируем логику join+enqueue
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("Зайди в войс! 🚁")
        return
    # ensure vc
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        try:
            await interaction.user.voice.channel.connect()
        except Exception as e:
            await interaction.followup.send(f"❌ Не зашла: {e}")
            return
    await _music_enqueue(dummy, query)
    # _music_enqueue уже ответит через dummy.reply -> followup

@bot.tree.command(name="stop", description="Остановить и выйти из войса")
async def slash_stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        _music_queues[interaction.guild.id].clear()
        _now_playing.pop(interaction.guild.id, None)
        await vc.disconnect()
        await interaction.response.send_message("🚁 Улетела! Пока! 💋")
    else:
        await interaction.response.send_message("Я не в войсе", ephemeral=True)

@bot.tree.command(name="skip", description="Пропустить трек")
async def slash_skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("⏭️ Скипнула! 🚁")
    else:
        await interaction.response.send_message("Нечего скипать", ephemeral=True)

# ============ BOT APPEARANCE (имя/аватар) ============
@bot.tree.command(name="set-name", description="Сменить глобальное имя бота (только админ, лимит 2/час)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(name="Новое имя бота (2-32 символа)")
async def set_bot_name(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        await bot.user.edit(username=name)
        await interaction.followup.send(f"✅ Имя бота сменено на **{name}** (глобально, кэш обновится до часа).", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Не смогла: {e.text if hasattr(e,'text') else e} (лимит 2 раза в час)", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

@bot.tree.command(name="set-nick", description="Сменить ник бота на этом сервере")
@app_commands.checks.has_permissions(manage_nicknames=True)
@app_commands.describe(nick="Новый ник (пусто — сбросить)")
async def set_bot_nick(interaction: discord.Interaction, nick: str = None):
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.guild.me.edit(nick=nick if nick else None)
        await interaction.followup.send(f"✅ Ник на сервере сменен на **{nick or 'по умолчанию'}**", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

@bot.tree.command(name="set-avatar", description="Сменить аватарку бота (только админ)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(image="Картинка (загрузи файл)")
async def set_bot_avatar(interaction: discord.Interaction, image: discord.Attachment):
    await interaction.response.defer(ephemeral=True)
    try:
        if image.size > 8_000_000:
            await interaction.followup.send("❌ Файл слишком большой (макс 8МБ)", ephemeral=True)
            return
        data = await image.read()
        await bot.user.edit(avatar=data)
        await interaction.followup.send("✅ Аватарка обновлена! Обнови кэш Ctrl+R.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

# Глобальный обработчик ошибок slash-команд — чтобы не было "Приложение не отвечает"
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"App command error: {error} | command={interaction.command.name if interaction.command else '?'}")
    try:
        msg = f"❌ Ошибка: {error}"
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Кулдаун, попробуй через {error.retry_after:.1f}с"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except:
        pass

# Квест Димы полностью удален по запросу

# Обработка ошибок прав
@setup_verify.error
@setup_base.error
@setup_roles.error
@setup_menu.error
@publish_rules.error
@clear.error
@kick.error
@ban.error
@mute.error
async def perm_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ У тебя нет прав для этой команды.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Ошибка: {error}", ephemeral=True)

# ============ WEB PANEL (сайт для настройки промпта) ============
def _start_panel():
    if os.getenv("PANEL_ENABLED", "true").lower() in ("0","false","no","off"):
        return
    try:
        from threading import Thread
        def run():
            try:
                from panel import app as panel_app
                port = int(os.getenv("PORT", "8080"))
                print(f"[Panel] старт на 0.0.0.0:{port}")
                panel_app.run(host="0.0.0.0", port=port, use_reloader=False)
            except Exception as e:
                print(f"[Panel] fail: {e}")
        t = Thread(target=run, daemon=True)
        t.start()
    except Exception as e:
        print(f"panel thread fail: {e}")

_start_panel()

# ============ RUN ============
if not config.TOKEN:
    print("❌ DISCORD_TOKEN не найден в .env !")
else:
    bot.run(config.TOKEN)
