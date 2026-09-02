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

# прогрев opus — иначе FFmpeg виснет и "has not terminated" в логах 17:28
try:
    import discord.opus
    if not discord.opus.is_loaded():
        try:
            discord.opus.load_opus("libopus.so.0")
        except:
            try:
                discord.opus.load_opus("opus")
            except:
                pass
    print(f"opus loaded: {discord.opus.is_loaded()}")
except Exception as e:
    print(f"opus load fail: {e}")

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
        "AI_MAX_HISTORY": int(os.getenv("AI_MAX_HISTORY", "25") or 25),
        "AI_COOLDOWN": int(os.getenv("AI_COOLDOWN", "5") or 5),
    }
    return _env_map.get(key, default)

_ai_history: dict[int, deque] = {}
_ai_cooldown: dict[int, float] = {}
_ai_client = None

def _get_ai_history(channel_id: int) -> deque:
    """Возвращает историю канала с динамическим maxlen и делает ГЛОБАЛЬНУЮ авто-очистку после 25 сообщений на ВСЕХ каналах сразу."""
    max_hist = _cfg("AI_MAX_HISTORY", 25)
    try:
        max_hist = int(max_hist)
    except:
        max_hist = 25
    maxlen = max_hist * 2  # user + assistant на каждый диалог
    # синхронизируем maxlen у всех каналов если настройка изменилась
    if _ai_history:
        for cid, h in list(_ai_history.items()):
            if h.maxlen != maxlen:
                _ai_history[cid] = deque(h, maxlen=maxlen)
    hist = _ai_history.get(channel_id)
    if hist is None:
        hist = deque(maxlen=maxlen)
        _ai_history[channel_id] = hist
        return hist
    if hist.maxlen != maxlen:
        new_hist = deque(hist, maxlen=maxlen)
        _ai_history[channel_id] = new_hist
        hist = new_hist
    # === ГЛОБАЛЬНАЯ АВТО-ОЧИСТКА ПОСЛЕ 25 СООБЩЕНИЙ НА ВСЕХ КАНАЛАХ СРАЗУ ===
    # Когда ЛЮБОЙ канал достиг лимита (25 диалогов = 50 записей) — чистим ПОЛНОСТЬЮ ВСЕ каналы
    if len(hist) >= maxlen and maxlen > 0:
        total = len(_ai_history)
        for h in _ai_history.values():
            h.clear()
        print(f"[AI] ГЛОБАЛЬНАЯ авто-очистка: канал-триггер {channel_id} достиг {max_hist} диалогов -> очищены ВСЕ {total} каналов")
    return _ai_history[channel_id]

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
    # история канала (с авто-очисткой полностью после 25 сообщений)
    hist = _get_ai_history(channel_id)
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
            # пробуем обойти 429 другой API: если был opencode — пробуем OpenRouter free, и наоборот
            try:
                alt_base = "https://api.openai.com/v1" if is_opencode else "https://opencode.ai/zen/v1/responses"
                alt_model = "openai/gpt-oss-20b:free" if is_opencode else "muse-spark-1.2-contributor-free"
                # если уже пробовали — не зацикливаем
                if alt_model != model:
                    print(f"429 fallback {model} -> {alt_model}")
                    # рекурсивный вызов с другим base (экономим)
                    # временно подменяем cfg через env
                    os.environ["AI_BASE_URL"]=alt_base
                    os.environ["AI_MODEL"]=alt_model
                    # один ретрай
                    return await _ask_ai(prompt, channel_id, author_name, author_id, guild_name)
            except Exception as e2:
                print(f"429 fallback fail: {e2}")
            return "⏳ AI перегружен (429) — лимит free 50/день (1000/день после $10 на OpenRouter). Попробуй через минуту или смени API: Variables AI_BASE_URL= https://api.groq.com/openai/v1 с ключом gsk_  (Groq лимиты выше) или кинь $10 на OpenRouter."
        return f"❌ Ошибка AI: {err[:400]}"

# ============ МУЗЫКА (Анечка зайди / включи) ============
_music_queues: dict[int, deque] = defaultdict(deque)  # guild_id -> deque of {url, title, requester}
_now_playing: dict[int, dict] = {}
_music_volume: dict[int, float] = defaultdict(lambda: 0.5)  # guild_id -> 0.0-2.0 (50% по дефолту)
_music_bass: dict[int, bool] = defaultdict(bool)  # guild_id -> bass boost вкл/выкл
_music_cmd_cooldown: dict[tuple, float] = {}  # (guild_id, user_id, cmd) -> last_ts
_processed_music_ids: set[int] = set()  # анти-дубль если Railway поднял 2 контейнера/эвент дублируется

FFMPEG_OPTIONS = {"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1 -rw_timeout 15000000 -probesize 32K -analyzeduration 0 -protocol_whitelist file,http,https,tcp,tls,crypto", "options": "-vn -c:a pcm_s16le -ar 48000 -ac 2 -loglevel warning"}
FFMPEG_BASS_OPTIONS = {"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1 -rw_timeout 15000000 -probesize 32K -analyzeduration 0 -protocol_whitelist file,http,https,tcp,tls,crypto", "options": "-vn -c:a pcm_s16le -ar 48000 -ac 2 -af bass=g=6:frequency=110:width=0.6 -loglevel warning"}

def _check_voice_permission(msg: discord.Message) -> bool:
    """Проверка: автор в том же войсе что и бот. Если нет — возвращаем False (отправлять reply должен вызыватель)."""
    vc = msg.guild.voice_client if msg.guild else None
    if not vc or not vc.is_connected():
        return False
    if not msg.author.voice or not msg.author.voice.channel:
        return False
    return msg.author.voice.channel.id == vc.channel.id

def _music_is_anechka(text_lower: str) -> bool:
    # чтобы "анечка" срабатывала и с "анечка," "анечка " и если пинг
    return "анечка" in text_lower or "анечка" in text_lower.replace("ё","е")

async def _music_join(msg: discord.Message, silent: bool = False) -> bool:
    if not msg.author.voice or not msg.author.voice.channel:
        await msg.reply("🚁 Зайди сначала в голосовой канал, брат! *хик* — я не знаю куда лететь.", mention_author=False)
        return False
    channel = msg.author.voice.channel
    try:
        perms = channel.permissions_for(msg.guild.me)
        if not perms.connect or not perms.speak:
            await msg.reply("❌ Нет прав зайти в войс: нужен `Connect` + `Speak` для моей роли на этом канале.", mention_author=False)
            return False
    except:
        pass
    vc = msg.guild.voice_client
    # если висит зомби-клиент (как в логе 17:28 handshake terminated + ffmpeg has not terminated) — форс-дисконнект
    if vc and not vc.is_connected():
        try:
            await vc.disconnect(force=True)
            await asyncio.sleep(1)
        except:
            pass
        vc = msg.guild.voice_client
    try:
        if vc and vc.is_connected():
            if vc.channel.id == channel.id:
                return True
            try:
                await vc.move_to(channel)
                return True
            except Exception as e:
                if "Already connected" in str(e):
                    return True
                print(f"_music_join move_to fail: {type(e).__name__}: {e}")
                # если move_to рвет хендшейк — пересоздаем
                try:
                    await vc.disconnect(force=True)
                    await asyncio.sleep(1)
                    await channel.connect(self_deaf=False, timeout=15, reconnect=True)
                    return True
                except Exception as e2:
                    print(f"_music_join move_to retry fail: {e2}")
                    raise e2
        else:
            # если уже есть неконнектящийся vc — убиваем
            if vc:
                try:
                    await vc.disconnect(force=True)
                    await asyncio.sleep(1)
                except:
                    pass
            try:
                await channel.connect(self_deaf=False, timeout=15, reconnect=True)
                # ждем stable коннекта 1с — иначе сразу play рвет хендшейк как в логе 17:28:00
                await asyncio.sleep(1)
                vc2 = msg.guild.voice_client
                if vc2 and vc2.is_connected():
                    return True
                print(f"_music_join: after connect still not connected")
                return False
            except Exception as e:
                if "Already connected" in str(e):
                    return True
                print(f"_music_join connect fail: {type(e).__name__}: {e}")
                raise
    except Exception as e:
        if "Already connected" in str(e):
            return True
        err = str(e).strip() or type(e).__name__
        details = err
        if "403" in err or "Forbidden" in err:
            details += " (нет прав Connect/Speak)"
        if "timed out" in err.lower() or "timeout" in err.lower() or "handshake" in err.lower():
            details += " (таймаут войса — Discord UDP лагает, попробуй ещё раз через 5с; если повторяется — перезайди в канал `Пещера из трупиков`)"
        print(f"_music_join final fail: {details}")
        await msg.reply(f"❌ Не смогла зайти в <#{channel.id}>: {details}", mention_author=False)
        return False

async def _music_leave(msg: discord.Message):
    vc = msg.guild.voice_client
    if not vc or not vc.is_connected():
        await msg.reply("Я и так не в войсе 😅", mention_author=False)
        return
    _music_queues[msg.guild.id].clear()
    _now_playing.pop(msg.guild.id, None)
    # чистим TTS очередь тоже
    try:
        _tts_queues[guild.id].clear()
    except:
        pass
    await vc.disconnect()
    await msg.reply("🚁 *улетаю*... пока, котик! 💋", mention_author=False)

# ============ TTS (edge-tts) — синтезатор речи Узи ============
_tts_queues: dict[int, deque] = defaultdict(deque)  # guild_id -> deque of {path, text, channel_id}
_tts_playing: dict[int, bool] = {}
import tempfile as _tts_tempfile

def _tts_clean(text: str) -> str:
    # убираем *действия*, эмодзи, глитчи — для озвучки
    t = re.sub(r"\*.*?\*", "", text)
    t = re.sub(r"//.*?//", "", t)
    t = re.sub(r"[◉💜💀🔫🤖]+", "", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"<@!?\d+>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 350:
        t = t[:350] + "…"
    return t

def _tts_play_next(guild: discord.Guild):
    q = _tts_queues[guild.id]
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        _tts_playing.pop(guild.id, None)
        return
    if not q:
        _tts_playing.pop(guild.id, None)
        # если музыка ждала — продолжить
        if _music_queues[guild.id] and not vc.is_playing():
            _music_play_next(guild)
        return
    item = q.popleft()
    _tts_playing[guild.id] = True
    path = item.get("path")
    if not path or not os.path.exists(path):
        print(f"tts play_next: bad path {path}")
        bot.loop.call_soon_threadsafe(lambda: _tts_play_next(guild))
        return
    # если музыка играет — ставим на паузу
    if vc.is_playing():
        try:
            vc.pause()
        except:
            pass
        # подождать 0.3с
        def _resume_and_play():
            try:
                vc.stop()
            except:
                pass
            _do_tts_play(guild, item, path)
        bot.loop.call_later(0.5, _resume_and_play)
    else:
        _do_tts_play(guild, item, path)

def _do_tts_play(guild: discord.Guild, item: dict, path: str):
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return
    try:
        source = discord.FFmpegPCMAudio(path, before_options="-probesize 32K -analyzeduration 0", options="-vn -loglevel warning")
        vol = _music_volume.get(guild.id, 0.5)
        pcm = discord.PCMVolumeTransformer(source, volume=min(1.2, vol + 0.3))
        def after_tts(err):
            # удалить файл
            try:
                os.remove(path)
            except:
                pass
            if err:
                print(f"tts after error: {err}")
            _tts_playing.pop(guild.id, None)
            # продолжить tts очередь или музыку
            bot.loop.call_soon_threadsafe(lambda: _tts_play_next(guild))
        # стопаем всё перед tts
        if vc.is_playing() or vc.is_paused():
            try:
                vc.stop()
            except:
                pass
        vc.play(pcm, after=after_tts)
        print(f"tts playing: {item.get('text','')[:60]} -> {path}")
    except Exception as e:
        print(f"tts play fail: {e}")
        try:
            os.remove(path)
        except:
            pass
        _tts_playing.pop(guild.id, None)
        bot.loop.call_soon_threadsafe(lambda: _tts_play_next(guild))

async def _tts_speak(guild: discord.Guild, text: str, channel_id: int | None = None):
    if not config.TTS_ENABLED:
        return
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return
    clean = _tts_clean(text)
    if not clean or len(clean) < 2:
        return
    # генерим mp3 через edge-tts
    try:
        import edge_tts
        voice = config.TTS_VOICE
        rate = config.TTS_RATE
        communicate = edge_tts.Communicate(clean, voice=voice, rate=rate)
        tf = _tts_tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tf.close()
        # таймаут 12с на генерацию
        await asyncio.wait_for(communicate.save(tf.name), timeout=12)
        if not os.path.exists(tf.name) or os.path.getsize(tf.name) < 500:
            print(f"tts gen empty: {tf.name}")
            try:
                os.remove(tf.name)
            except:
                pass
            return
        item = {"path": tf.name, "text": clean, "channel_id": channel_id}
        _tts_queues[guild.id].append(item)
        # если ничего не играет — стартуем сразу, иначе встанет в очередь
        vc2 = guild.voice_client
        if vc2 and not vc2.is_playing() and not _tts_playing.get(guild.id):
            _tts_play_next(guild)
        elif _tts_playing.get(guild.id):
            pass  # уже играет — в очереди
        else:
            # музыка играет — ставим tts в приоритет (пауза музыки и tts)
            if vc2 and vc2.is_playing() and not _tts_playing.get(guild.id):
                _tts_play_next(guild)
    except asyncio.TimeoutError:
        print(f"tts gen timeout for: {clean[:40]}")
    except Exception as e:
        print(f"tts gen fail: {type(e).__name__}: {e}")

# ============ STT (голос -> текст) ============
_stt_listening: dict[int, bool] = {}  # guild_id -> listening

async def _stt_transcribe(wav_path: str, lang: str = "ru") -> str:
    # 1) пробуем OpenAI Whisper API если есть ключ
    oai_key = os.getenv("OPENAI_API_KEY", "") or os.getenv("OPENCODE_API_KEY", "")
    # openai ключ начинается с sk-
    if oai_key and oai_key.startswith("sk-"):
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=oai_key)
            with open(wav_path, "rb") as f:
                tr = await client.audio.transcriptions.create(model=config.STT_MODEL, file=f, language=lang)
                return (tr.text or "").strip()
        except Exception as e:
            print(f"stt openai fail: {e}")
    # 2) локальный whisper (faster-whisper) если установлен — опционально
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(wav_path, language=lang)
        text = " ".join([s.text for s in segments]).strip()
        if text:
            return text
    except Exception:
        pass
    # 3) fallback — не настроено
    return ""

def _stt_sink_callback(sink, guild: discord.Guild, text_channel_id: int):
    # вызывается когда останавливаем запись
    bot.loop.create_task(_stt_process_sink(sink, guild, text_channel_id))

async def _stt_process_sink(sink, guild: discord.Guild, text_channel_id: int):
    ch = bot.get_channel(text_channel_id)
    # sink.audio_data = {user_id: AudioData}
    for user_id, audio in getattr(sink, "audio_data", {}).items():
        # пропускаем бота
        if user_id == bot.user.id:
            continue
        # сохраняем wav
        try:
            # audio.file — BytesIO wav
            import wave, io
            wav_path = _tts_tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
            # sink уже wav, просто скидываем
            with open(wav_path, "wb") as f:
                f.write(audio.file.getvalue())
            if os.path.getsize(wav_path) < 5000:
                os.remove(wav_path)
                continue
            await ch.send(f"🎤 Слышу <@{user_id}> — распознаю…")
            text = await _stt_transcribe(wav_path, config.STT_LANGUAGE)
            try:
                os.remove(wav_path)
            except:
                pass
            if not text or len(text) < 2:
                await ch.send(f"😶 Не расслышала <@{user_id}> — попробуй ещё раз, говори чётче")
                continue
            await ch.send(f"👂 <@{user_id}> сказал: *{text[:300]}*")
            # кормим в AI как будто он написал в чат
            # находим имя
            member = guild.get_member(user_id)
            name = member.display_name if member else f"User{user_id}"
            answer = await _ask_ai(text, text_channel_id, name, user_id, guild.name)
            if answer:
                await ch.send(answer, allowed_mentions=discord.AllowedMentions(users=False))
                # авто-озвучка
                if config.TTS_ENABLED and guild.voice_client and guild.voice_client.is_connected():
                    await _tts_speak(guild, answer, text_channel_id)
        except Exception as e:
            print(f"stt process fail user {user_id}: {e}")
            try:
                await ch.send(f"❌ STT ошибка: {e}")
            except:
                pass

async def _stt_start(guild: discord.Guild, text_channel_id: int, duration: int = 15):
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return False, "Бот не в войсе — `Узи зайди` сначала"
    if _stt_listening.get(guild.id):
        return False, "Уже слушаю — говори сейчас!"
    # проверка STT
    has_key = os.getenv("OPENAI_API_KEY", "").startswith("sk-")
    has_local = False
    try:
        import faster_whisper
        has_local = True
    except:
        pass
    if not has_key and not has_local:
        return False, "❌ STT не настроен: добавь `OPENAI_API_KEY=sk-...` в Railway Variables (Whisper) или установи `faster-whisper`. Пока добавь ключ — без него распознавания нет."
    # пробуем разные пути импорта (2.4/2.5/2.6 разные)
    WaveSink = None
    try:
        from discord.sinks import WaveSink as _WS
        WaveSink = _WS
    except Exception as e1:
        try:
            import discord.sinks
            WaveSink = discord.sinks.WaveSink
        except Exception as e2:
            try:
                from discord.sinks.wave import WaveSink as _WS2
                WaveSink = _WS2
            except Exception as e3:
                return False, f"❌ discord.sinks не доступен: {e3} (нужен discord.py[voice]>=2.5.2 + PyNaCl — сделай Redeploy с обновлённым requirements.txt, версия сейчас {getattr(__import__('discord'), '__version__', '?')})"
    try:
        sink = WaveSink()
        vc.start_recording(sink, lambda s: _stt_sink_callback(s, guild, text_channel_id), guild)
        _stt_listening[guild.id] = True
        # авто-стоп через duration
        async def _auto_stop():
            await asyncio.sleep(duration)
            if _stt_listening.get(guild.id):
                await _stt_stop(guild)
        bot.loop.create_task(_auto_stop())
        return True, f"🎤 Слушаю {duration}с — говори в микрофон! (язык {config.STT_LANGUAGE})"
    except Exception as e:
        return False, f"❌ Не смогла начать запись: {e}"

async def _stt_stop(guild: discord.Guild):
    vc = guild.voice_client
    if not vc:
        return False
    try:
        vc.stop_recording()
    except Exception as e:
        print(f"stt stop fail: {e}")
    _stt_listening.pop(guild.id, None)
    return True

def _music_play_next(guild: discord.Guild):
    q = _music_queues[guild.id]
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        print(f"music play_next: vc not connected guild={guild.id}")
        _now_playing.pop(guild.id, None)
        # пробуем вернуть трек в очередь чтобы не потерять
        return
    if not q:
        _now_playing.pop(guild.id, None)
        return
    item = q.popleft()
    _now_playing[guild.id] = item
    # валидация URL
    url = item.get("url", "")
    if not url or not url.startswith("http"):
        print(f"music play_next: bad url for '{item.get('title')}' url={url[:120]}")
        ch_id = item.get("channel_id")
        if ch_id:
            ch = bot.get_channel(ch_id)
            if ch:
                bot.loop.create_task(ch.send(f"❌ Не смог запустить **{item['title']}** — битая ссылка. Пробую следующий..."))
        bot.loop.call_soon_threadsafe(lambda: _music_play_next(guild))
        return
    try:
        use_bass = _music_bass.get(guild.id, False)
        opts = FFMPEG_BASS_OPTIONS if use_bass else FFMPEG_OPTIONS
        print(f"music play_next: FFmpeg {item['title'][:60]} url={url[:80]}... bass={use_bass} vol={_music_volume.get(guild.id,0.5)}")
        try:
            base_source = discord.FFmpegPCMAudio(url, **opts)
        except Exception as e_ff:
            print(f"music FFmpegPCMAudio fail: {e_ff} — пробую без bass")
            base_source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
        vol = _music_volume.get(guild.id, 0.5)
        source = discord.PCMVolumeTransformer(base_source, volume=vol)
        def after(err):
            if err:
                print(f"music after error: {err}")
                # уведомить в чат если ошибка воспроизведения
                ch_id2 = item.get("channel_id")
                if ch_id2:
                    ch2 = bot.get_channel(ch_id2)
                    if ch2:
                        bot.loop.create_task(ch2.send(f"⚠️ Ошибка воспроизведения **{item['title']}**: {err} → скипаю"))
            bot.loop.call_soon_threadsafe(lambda: _music_play_next(guild))
        # если уже что-то играет — стопаем
        if vc.is_playing() or vc.is_paused():
            try: vc.stop()
            except: pass
        vc.play(source, after=after)
        ch_id = item.get("channel_id")
        if ch_id:
            ch = bot.get_channel(ch_id)
            if ch:
                bot.loop.create_task(ch.send(f"🎧 Сейчас играет: **{item['title']}** — заказал {item['requester']}"))
    except Exception as e:
        print(f"music play_next fail: {type(e).__name__}: {e}")
        ch_id = item.get("channel_id")
        if ch_id:
            ch = bot.get_channel(ch_id)
            if ch:
                bot.loop.create_task(ch.send(f"❌ Ошибка запуска **{item['title']}**: {e}"))
        _now_playing.pop(guild.id, None)
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
    # FIX для "Requested format is not available" (SABR/PO-Token) — см. логи 17:08:53 vVQXkBDbG1E
    import tempfile
    class _SilentLogger:
        def debug(self, msg): pass
        def warning(self, msg): pass
        def error(self, msg): pass
        def info(self, msg): pass
    base_ydl_opts = {"noplaylist": True, "quiet": True, "no_warnings": True, "ignoreerrors": False, "ignore_no_formats_error": True, "logger": _SilentLogger(), "default_search": "ytsearch1", "extract_flat": False, "skip_download": True, "nocheckcertificate": True, "retries": 1, "fragment_retries": 1, "extractor_retries": 0, "socket_timeout": 10, "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}}
    # было 7 клиентов ×5 форматов = 35 на каждый видос → тормоза 30-60с. Теперь быстро: 2 клиента ×2 формата
    client_sets = [["android"], ["web"]]
    format_tries = [None, "bestaudio/best"]
    cookies_data = os.getenv("YT_COOKIES", "")
    ydl_opts_base = base_ydl_opts.copy()
    if cookies_data and "netscape" in cookies_data.lower():
        try:
            tf = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
            tf.write(cookies_data)
            tf.close()
            ydl_opts_base["cookiefile"] = tf.name
        except:
            pass
        client_sets = [["web"], ["android"]]
    # обогащаем короткие/генитивные запросы чтобы ytsearch находил музыку, а не болтовню
    _ql = query.lower().strip()
    search_queries = [query]
    if _ql in ("фиксиков", "фиксики", "фикс", "фикси", "fixiki", "fixies") or _ql.startswith("фиксик"):
        search_queries = [query + " песня", query + " музыка", query, "Фиксики песенка"]
    elif _ql in ("дронов", "дрон", "дроны", "дронов убийц", "дроны убийцы"):
        search_queries = ["Murder Drones OST", "Murder Drones music", query + " OST", query]
    elif len(_ql.split()) == 1 and len(_ql) <= 7:
        # одно короткое слово типа "дронов" — добавляем музыку
        search_queries = [query + " OST music", query + " песня", query]

    def _pick_best_url(info):
        # 1) прямой url выбранный yt-dlp — но проверяем что это не веб-страница/сторборд
        direct = info.get("url")
        if direct and direct.startswith("http") and "youtube.com/watch" not in direct and "youtu.be/" not in direct and "i.ytimg.com" not in direct and "storyboard" not in direct:
            if "googlevideo" in direct or "manifest.googlevideo" in direct or direct.endswith((".m4a",".mp3",".webm",".opus",".m3u8")) or "mime=audio" in direct:
                return direct
        # 2) requested_formats (когда format = bv+ba)
        if info.get("requested_formats"):
            # приоритет чисто аудио
            for f in info["requested_formats"]:
                if f.get("url") and f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none"):
                    if f["url"].startswith("http"):
                        return f["url"]
            for f in info["requested_formats"]:
                if f.get("url") and f.get("acodec") not in (None, "none"):
                    return f["url"]
            for f in info["requested_formats"]:
                if f.get("url"):
                    return f["url"]
        # 3) formats — вручную выбираем лучший аудио, избегаем HLS и сторбордов (как в логе 17:48 i.ytimg.com/sb/...L2/M$M.jpg)
        def _is_bad_url(u: str) -> bool:
            bad = ("i.ytimg.com" in u or "storyboard" in u or u.endswith(".jpg") or u.endswith(".webp") or "/sb/" in u or "M$M.jpg" in u)
            return bad
        fmts = [f for f in (info.get("formats") or []) if f.get("url") and f["url"].startswith("http") and not _is_bad_url(f["url"])]
        # также режем по protocol mhtml
        fmts = [f for f in fmts if f.get("protocol") not in ("mhtml",) and f.get("ext") not in ("mhtml",)]
        if not fmts:
            return direct if direct and direct.startswith("http") and not _is_bad_url(direct) else None
        # отделяем HLS
        audio = [f for f in fmts if f.get("acodec") not in (None, "none")]
        # предпочитаем не-hls (googlevideo direct) над hls (m3u8)
        direct_audio = [f for f in audio if "manifest.googlevideo" not in f["url"] and ".m3u8" not in f["url"]]
        pick_pool = direct_audio if direct_audio else audio
        if not pick_pool:
            # если осталось только видео+аудио — режем storyboard уже, но не возвращаем jpg
            pick_pool = [f for f in fmts if not _is_bad_url(f["url"])] or fmts
        # приоритет m4a/opus/webm аудио, избегаем video+audio миксы
        pure_audio = [f for f in pick_pool if f.get("vcodec") in (None, "none")]
        if pure_audio:
            pick_pool = pure_audio
        def _score(f):
            # opus/m4a выше, выше abr
            ext_bonus = 10 if f.get("ext") in ("m4a","opus","webm") else 0
            return (ext_bonus, f.get("abr") or 0, f.get("tbr") or 0, f.get("acodec") != "none", f.get("height") or 0)
        try:
            pick_pool_sorted = sorted(pick_pool, key=_score, reverse=True)
            return pick_pool_sorted[0]["url"]
        except:
            return pick_pool[-1]["url"]

    # сразу показываем что ищем — чтобы не казалось что зависло
    status_msg = None
    try:
        status_msg = await msg.reply(f"🔍 Ищу `{query}`...", mention_author=False)
    except:
        pass
    loop = asyncio.get_event_loop()
    def _search_entries_for(candidates):
        last = None
        for sq in candidates:
            for clients in client_sets:
                search_opts = {"noplaylist": True, "quiet": True, "no_warnings": True, "ignoreerrors": True, "logger": _SilentLogger(), "extract_flat": False, "skip_download": True, "nocheckcertificate": True, "socket_timeout": 10}
                if "cookiefile" in ydl_opts_base:
                    search_opts["cookiefile"] = ydl_opts_base["cookiefile"]
                search_opts["extractor_args"] = {"youtube": {"player_client": clients}}
                try:
                    with yt_dlp.YoutubeDL(search_opts) as ydl_search:
                        search = ydl_search.extract_info(f"ytsearch5:{sq}", download=False)
                        entries = (search.get("entries") or []) if search else []
                        entries = [e for e in entries if e and e.get("id") and e.get("id") != "vVQXkBDbG1E"]
                        if entries:
                            print(f"ytsearch ok: '{sq}' via {clients} -> {len(entries)} entries")
                            return entries
                        else:
                            print(f"ytsearch empty: '{sq}' via {clients}")
                except Exception as e:
                    last = e
                    print(f"ytsearch fail: '{sq}' via {clients}: {e}")
                    continue
        if last:
            print(f"ytsearch final fail: {last}")
        return None

    def _extract():
        last_err = None
        # если прямая ссылка — пробуем сразу с каждым клиентом/форматом, выбирая url вручную
        if query.startswith("http"):
            for clients in client_sets:
                for fmt in format_tries:
                    opts = ydl_opts_base.copy()
                    opts["extractor_args"] = {"youtube": {"player_client": clients}}
                    if fmt is None:
                        opts.pop("format", None)
                    else:
                        opts["format"] = fmt
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(query, download=False)
                            url_test = _pick_best_url(info)
                            if url_test:
                                info["url"] = url_test
                                return info
                            if info.get("formats"):
                                return info
                    except Exception as e:
                        last_err = e
                        msg = str(e)
                        if "vVQXkBDbG1E" in msg:
                            continue
                        if "Requested format is not available" in msg or "format is not available" in msg.lower():
                            continue
                        if "Sign in" in msg or "cookies" in msg.lower():
                            break
                        continue
            if last_err:
                raise last_err
            return None

        search_entries = _search_entries_for(search_queries)
        if not search_entries:
            # фолбэк 1: пробуем без обогащения, просто исходный запрос через android
            if search_queries != [query]:
                search_entries = _search_entries_for([query])
            if not search_entries:
                # фолбэк 2: SoundCloud
                try:
                    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": _SilentLogger(), "extract_flat": False, "skip_download": True}) as ydl2:
                        s = ydl2.extract_info(f"scsearch1:{query}", download=False)
                        entries_sc = []
                        if s:
                            if s.get("entries"):
                                entries_sc = [e for e in s.get("entries") if e]
                            elif s.get("url") or s.get("formats"):
                                return s
                        if entries_sc:
                            search_entries = entries_sc
                        else:
                            print(f"scsearch empty for '{query}'")
                except Exception as e_sc:
                    print(f"scsearch fail: {e_sc}")
            if not search_entries:
                if last_err:
                    print(f"search final fail raise: {last_err}")
                    raise last_err
                raise Exception(f"Ничего не нашлось по '{query}' (ytsearch вернул 0). Попробуй 'Узи включи {query} OST' или кинь прямую ссылку YouTube")

        def _is_music(e):
            t = (e.get("title") or "").lower()
            # жестко понижаем Roblox/SCP когда ищем дронов — это был баг на скрине
            ql = query.lower()
            if ("drone" in ql or "дрон" in ql or "murder" in ql) and ("scp" in t or "roblox" in t):
                return False
            music_kw = ["ost", "music", "song", "soundtrack", "audio", "official", "mv", "m/v", "amv", "nightcore", "cover", "remix", "phonk", "trap", "lofi", "instrumental", "theme", "opening", "ending", "песн", "музык"]
            if any(k in t for k in music_kw):
                return True
            # смягчаем фильтр: для дронов/фиксиков не отсеиваем жестко, только сортируем
            if "дрон" in ql or "murder" in ql:
                return any(k in t for k in ["ost", "music", "soundtrack", "song", "cover", "remix", "phonk"]) or True
            if "грустн" in ql or "atmospheric" in ql or "sad" in ql:
                return True
            if "фиксик" in ql:
                return any(k in t for k in ["песн", "музык", "song", "music", "караоке", "сборник"]) or True
            return True
        entries_sorted = sorted(search_entries, key=lambda e: 0 if _is_music(e) else 1)
        # смещаем проблемный vVQXkBDbG1E и Roblox/SCP вниз
        entries_sorted = sorted(entries_sorted, key=lambda e: (1 if e.get("id")=="vVQXkBDbG1E" else 0, 1 if "scp" in (e.get("title") or "").lower() or "roblox" in (e.get("title") or "").lower() else 0))

        for entry in entries_sorted:
            if not entry:
                continue
            vid = entry.get("id")
            if vid == "vVQXkBDbG1E":
                continue
            url = entry.get("webpage_url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None) or entry.get("url")
            if not url:
                continue
            dt = (entry.get("title") or "").lower()
            if ("вернулись" in dt and "сезон" in dt and "murder drones" in dt and "ost" not in dt and "music" not in dt):
                continue
            for clients in client_sets:
                for fmt in format_tries:
                    opts = ydl_opts_base.copy()
                    opts["extractor_args"] = {"youtube": {"player_client": clients}}
                    if fmt is None:
                        opts.pop("format", None)
                    else:
                        opts["format"] = fmt
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            detail = ydl.extract_info(url, download=False)
                            if not detail:
                                continue
                            best = _pick_best_url(detail)
                            if best:
                                detail["url"] = best
                                dtitle = (detail.get("title") or dt).lower()
                                if ("вернулись" in dtitle and "сезон" in dtitle and "murder drones" in dtitle and "ost" not in dtitle and "music" not in dtitle):
                                    last_err = Exception(f"skip non-music: {dtitle[:60]}")
                                    continue
                                return detail
                            if detail.get("formats"):
                                return detail
                    except Exception as e2:
                        last_err = e2
                        if "vVQXkBDbG1E" in str(e2):
                            continue
                        if "Requested format is not available" in str(e2) or "format is not available" in str(e2).lower():
                            continue
                        if "Sign in" in str(e2) or "cookies" in str(e2).lower():
                            break
                        continue
        if entries_sorted:
            first = entries_sorted[0]
            vid = first.get("id")
            if vid == "vVQXkBDbG1E" and len(entries_sorted) > 1:
                first = entries_sorted[1]
                vid = first.get("id")
            url = first.get("webpage_url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None)
            if url:
                try:
                    with yt_dlp.YoutubeDL({**ydl_opts_base, "extractor_args": {"youtube": {"player_client": ["android"]}}}) as ydl:
                        detail = ydl.extract_info(url, download=False)
                        best = _pick_best_url(detail) if detail else None
                        if best:
                            detail["url"] = best
                            return detail
                except Exception as e:
                    last_err = e
        if last_err:
            # прячем спамный стек youtube в понятное сообщение
            msg = str(last_err)
            if "vVQXkBDbG1E" in msg or "Requested format" in msg:
                raise Exception("YouTube не отдал аудио (SABR/PO-Token). Попробуй другой запрос, например 'Узи включи Фиксики песня', или добавь YT_COOKIES в Railway Variables")
            raise last_err
        return entries_sorted[0] if entries_sorted else None

    try:
        # таймаут 20с на весь extract чтобы не висеть минуту как на скрине
        info = await asyncio.wait_for(loop.run_in_executor(None, _extract), timeout=20)
    except asyncio.TimeoutError:
        try:
            if status_msg: await status_msg.edit(content="❌ Долго ищет — YouTube тормозит. Попробуй короче: `Узи включи фиксики песня` или кинь ссылку.")
            else: await msg.reply("❌ Таймаут поиска (20с). Попробуй другой запрос.", mention_author=False)
        except: pass
        return
    except Exception as e:
        try:
            if status_msg: await status_msg.edit(content=f"❌ Не нашла: {e}")
            else: await msg.reply(f"❌ Не нашла: {e}", mention_author=False)
        except: pass
        return
    if not info:
        try:
            if status_msg: await status_msg.edit(content="❌ Ничего не нашла по запросу.")
            else: await msg.reply("❌ Ничего не нашла по запросу.", mention_author=False)
        except: pass
        return
    # финальный выбор аудио url — уже с фолбэком на ручной пик
    url = _pick_best_url(info)
    if not url:
        url = info.get("url") or (info.get("entries", [{}])[0].get("url") if info.get("entries") else None)
    if not url and info.get("formats"):
        fmts = [f for f in info["formats"] if f.get("url")]
        audio = [f for f in fmts if f.get("acodec") != "none"]
        pick = None
        if audio:
            try:
                pick = max(audio, key=lambda f: (f.get("abr") or f.get("tbr") or 0))
            except:
                pick = audio[-1]
        else:
            pick = fmts[-1] if fmts else None
        url = pick.get("url") if pick else None
    if not url:
        try:
            if status_msg: await status_msg.edit(content="❌ Не смогла вытащить аудио (формат недоступен). Попробуй прямую ссылку.")
            else: await msg.reply(f"❌ Не смогла вытащить аудио (формат недоступен). Попробуй прямую ссылку.", mention_author=False)
        except: pass
        return
    title = info.get("title", query)[:150]
    item = {"url": url, "title": title, "requester": msg.author.mention, "channel_id": msg.channel.id, "webpage": info.get("webpage_url", "")}
    _music_queues[msg.guild.id].append(item)
    vc = msg.guild.voice_client
    # удаляем "Ищу..." и показываем результат
    try:
        if status_msg: await status_msg.delete()
    except: pass
    if vc.is_playing() or vc.is_paused():
        await msg.reply(f"✅ В очередь: **{title}** (#{len(_music_queues[msg.guild.id])})", mention_author=False)
    else:
        await msg.reply(f"🔍 Нашла **{title}** — врубаю! 🚁💿", mention_author=False)
        _music_play_next(msg.guild)

async def _handle_music_triggers(message: discord.Message) -> bool:
    """Возвращает True если это музыкальная команда и уже обработана (не надо в AI). Поддерживает Анечка/Узи/Uzi. Только те кто в войсе с ботом могут управлять."""
    if not message.guild or message.author.bot:
        return False
    # анти-дубль: если тот же message.id уже обрабатывали (два контейнера/двойной on_message) — игнор
    if message.id in _processed_music_ids:
        return True
    _processed_music_ids.add(message.id)
    if len(_processed_music_ids) > 2000:
        # чистим старые, оставляем последние 1000
        try:
            _processed_music_ids.clear()
        except:
            pass
    low = message.content.lower().strip()
    low_clean = re.sub(rf"<@!?{bot.user.id}>" if bot.user else r"<@!?\d+>", "", low).strip() if bot.user else low
    def _start_any(names, *suffixes):
        for n in names:
            for s in suffixes:
                if low_clean.startswith(f"{n} {s}") or low_clean == f"{n} {s}":
                    return True
        return False
    names = ["анечка", "узи", "uzi"]
    def _voice_denied():
        vc = message.guild.voice_client if message.guild else None
        if not vc or not vc.is_connected():
            return False  # бота нет — разрешаем (зайди/включи сами проверят)
        return not _check_voice_permission(message)
    # зайди — без проверки same channel, но надо быть в любом войсе (проверит _music_join)
    if _start_any(names, "зайди", "зайди ко мне", "зайди к нам", "го в войс", "го к нам", "присоединись", "зайди в войс"):
        await _music_join(message, silent=True)
        return True
    if _start_any(names, "выйди", "ливни", "ливнуть", "ливать", "покинь", "уйди", "выйди из войса", "ливни из войса"):
        # анти-спам: не флудить "Я и так не в войсе" чаще 3 сек на юзера
        key = (message.guild.id, message.author.id, "leave")
        now = time.time()
        if now - _music_cmd_cooldown.get(key, 0) < 3:
            return True
        _music_cmd_cooldown[key] = now
        if _voice_denied():
            await message.reply("🚫 Только те кто в войсе со мной могут выгнать — зайди в мой канал!", mention_author=False)
            return True
        # если бот уже не в войсе — отвечаем, но с кд выше уже защита от спама
        if not message.guild.voice_client or not message.guild.voice_client.is_connected():
            await message.reply("Я и так не в войсе 😅", mention_author=False)
            return True
        await _music_leave(message)
        return True
    # умные музыкальные триггеры: "узи включи/поставь/добавь/запусти/в очередь/плей" — теперь с AI-уточнением
    if any(low_clean.startswith(n) for n in names) and any(kw in low_clean for kw in ["включи","поставь","добавь","запусти","очередь","плей","play"]):
        if _voice_denied():
            await message.reply("🚫 Только те кто в войсе со мной могут ставить музыку — зайди в мой канал!", mention_author=False)
            return True
        q_raw = re.sub(r"^(?:анечка|узи|uzi)\s+(?:включи|поставь(?:\s+в\s+очередь)?|добавь(?:\s+в\s+очередь)?|запусти|плей|play)\s*", "", message.content, flags=re.I).strip()
        q_raw = re.sub(r"^(?:в\s+очередь\s*)", "", q_raw, flags=re.I).strip()
        q_raw = re.sub(rf"<@!?{bot.user.id}>", "", q_raw).strip() if bot.user else q_raw
        # 1) быстрый эвристический фикс для ветки дронов (без AI, 0мс) — "Ost Drone forever" -> Murder Drones
        q = q_raw
        ql = q.lower()
        if "drone" in ql and "murder" not in ql and "scp" not in ql and "roblox" not in ql:
            # Ost Drone forever / drone forever / дроны — считаем что хотят Murder Drones
            q = re.sub(r"(?i)\b(ost\s*)?drones?\b", "Murder Drones", q).strip()
            if "murder drones" not in q.lower():
                q = "Murder Drones " + q
            q = re.sub(r"\s+", " ", q).strip()
        # 2) AI-уточнение (Opencode Zen) — если настроен, спросим оптимальный YouTube запрос (до 2 сек)
        api_key = _cfg("AI_API_KEY", "")
        base_url = _cfg("AI_BASE_URL", "https://opencode.ai/zen/v1/responses")
        if api_key and "opencode.ai" in base_url and len(q_raw) >= 2:
            try:
                import aiohttp
                payload = {
                    "model": "muse-spark-1.2-contributor-free",
                    "instructions": "Ты поисковик музыки. По запросу пользователя верни ОДНОЙ строкой оптимальный YouTube запрос 2-6 слов для ytsearch. Для вселенной Murder Drones всегда добавляй 'Murder Drones'. Для 'Ost Drone forever' -> 'Murder Drones Forever OST'. Для 'фиксики большой секрет' -> 'Фиксики Большой секрет песня'. Для простого 'дронов' -> 'Murder Drones OST'. Если уже точный запрос — верни его как есть. Не добавляй лишнего.",
                    "input": q_raw,
                    "temperature": 0.3,
                    "max_output_tokens": 30
                }
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(base_url, json=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=2.5)) as r:
                        if r.status == 200:
                            data = await r.json()
                            ai_q = None
                            if data.get("output"):
                                for out in data["output"]:
                                    if out.get("content"):
                                        for c in out["content"]:
                                            if c.get("text"):
                                                ai_q = c["text"]
                                                break
                                    if ai_q: break
                            ai_q = (ai_q or data.get("output_text") or "").strip().replace('"','').replace("'","").strip()
                            if ai_q and 2 <= len(ai_q) <= 80 and len(ai_q.split()) <= 8:
                                # защита: если AI вернул Roblox/SCP когда просили дронов — игнор
                                if "drone" in q_raw.lower() and "roblox" in ai_q.lower():
                                    print(f"AI refine ignored Roblox for drone query: {ai_q}")
                                else:
                                    q = ai_q
                                    print(f"AI refine: '{q_raw}' -> '{q}'")
            except Exception as e:
                print(f"AI refine fail: {e}")
        if q and len(q) > 2:
            await _music_enqueue(message, q)
            return True
        await _music_enqueue(message, q_raw)
        return True
    # TTS: "Узи скажи/говори/озвучь <текст>" — синтез речи в войсе
    if any(low_clean.startswith(n) for n in names) and any(kw in low_clean for kw in ["скажи","говори","озвучь","произнеси","сказать"]):
        if not config.TTS_ENABLED:
            await message.reply("🔇 TTS выключен (TTS_ENABLED=false).", mention_author=False)
            return True
        q_tts = re.sub(r"^(?:анечка|узи|uzi)\s+(?:скажи|говори|озвучь|произнеси|сказать)\s*", "", message.content, flags=re.I).strip()
        q_tts = re.sub(rf"<@!?{bot.user.id}>", "", q_tts).strip() if bot.user else q_tts
        if not q_tts:
            await message.reply("Что сказать? Напиши: `Узи скажи привет, я Узи!`", mention_author=False)
            return True
        vc = message.guild.voice_client
        if not vc or not vc.is_connected():
            ok = await _music_join(message)
            if not ok:
                return True
        await message.reply(f"🗣️ Озвучиваю: *{q_tts[:120]}*", mention_author=False)
        await _tts_speak(message.guild, q_tts, message.channel.id)
        return True
    # STT: "Узи слушай/послушай [сек]" — начать распознавание голоса
    if any(low_clean.startswith(n) for n in names) and any(kw in low_clean for kw in ["слушай","послушай","слушать","слушай меня"]):
        # парсим длительность
        m = re.search(r"(\d+)", low_clean)
        dur = int(m.group(1)) if m else 15
        dur = max(5, min(dur, 60))
        vc = message.guild.voice_client
        if not vc or not vc.is_connected():
            ok = await _music_join(message)
            if not ok:
                return True
        ok, msg_text = await _stt_start(message.guild, message.channel.id, dur)
        await message.reply(msg_text, mention_author=False)
        return True
    if low_clean.startswith(tuple(f"{n} стоп слуш" for n in names)) or low_clean.startswith(tuple(f"{n} хватит слуш" for n in names)):
        await _stt_stop(message.guild)
        await message.reply("🛑 Перестала слушать", mention_author=False)
        return True
    if _start_any(names, "стоп", "пауза"):
        if _voice_denied():
            await message.reply("🚫 Зайди в мой войс чтобы ставить на паузу!", mention_author=False)
            return True
        vc = message.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await message.reply("⏸️ Пауза, котик... *винты замедляются* 🚁", mention_author=False)
        else:
            await message.reply("Нечего ставить на паузу.", mention_author=False)
        return True
    if _start_any(names, "продолжи", "резюме", "play"):
        if _voice_denied():
            await message.reply("🚫 Зайди в мой войс чтобы продолжить!", mention_author=False)
            return True
        vc = message.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await message.reply("▶️ Поехали дальше! 🚁💨", mention_author=False)
        else:
            await message.reply("Нечего продолжать.", mention_author=False)
        return True
    if _start_any(names, "скип", "дальше", "пропусти", "next", "скипни"):
        if _voice_denied():
            await message.reply("🚫 Только в войсе можно скипать!", mention_author=False)
            return True
        vc = message.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await message.reply("⏭️ Скипаю... 🚁", mention_author=False)
        else:
            await message.reply("Очередь пуста.", mention_author=False)
        return True
    if _start_any(names, "очередь", "что играет", "что сейчас играет"):
        await message.reply((lambda: (lambda q,now: (f"▶️ Сейчас: **{now['title']}**\n" if now else "") + ("\n".join([f"{i}. {it['title']}" for i,it in enumerate(list(q)[:10],1)]) if q else "Очередь пуста.")))(_music_queues[message.guild.id], _now_playing.get(message.guild.id)) or "Тихо...", mention_author=False)
        return True
    # громкость/басы — умные, понимают "еще прибавь", "добавь бассов" и т.д.
    if any(low_clean.startswith(n) for n in names) and any(kw in low_clean for kw in ["убавь", "тише", "потише", "убавь громкость", "убавь звук"]):
        if _voice_denied():
            await message.reply("🚫 Зайди в мой войс чтобы менять громкость!", mention_author=False)
            return True
        gid = message.guild.id
        cur = _music_volume.get(gid, 0.5)
        new = max(0.0, cur - 0.15)
        _music_volume[gid] = new
        vc = message.guild.voice_client
        if vc and vc.source and hasattr(vc.source, "volume"):
            try: vc.source.volume = new
            except: pass
        await message.reply(f"🔉 Убавила → {int(new*100)}% (0-200%)", mention_author=False)
        return True
    if any(low_clean.startswith(n) for n in names) and any(kw in low_clean for kw in ["прибавь", "громче", "погромче", "громкость", "прибавь звук"]):
        if _voice_denied():
            await message.reply("🚫 Зайди в мой войс чтобы менять громкость!", mention_author=False)
            return True
        gid = message.guild.id
        cur = _music_volume.get(gid, 0.5)
        new = min(2.0, cur + 0.15)
        _music_volume[gid] = new
        vc = message.guild.voice_client
        if vc and vc.source and hasattr(vc.source, "volume"):
            try: vc.source.volume = new
            except: pass
        await message.reply(f"🔊 Прибавила → {int(new*100)}% 💜", mention_author=False)
        return True
    if any(low_clean.startswith(n) for n in names) and "бас" in low_clean:
        if any(w in low_clean for w in ["убери", "выключи", "без", "выкл", "убрать"]):
            if _voice_denied():
                await message.reply("🚫 Зайди в войс!", mention_author=False)
                return True
            _music_bass[message.guild.id] = False
            await message.reply("🎚️ Бассы убрала, чисто 🎧", mention_author=False)
            return True
        else:
            if _voice_denied():
                await message.reply("🚫 Только в войсе можно крутить басы!", mention_author=False)
                return True
            _music_bass[message.guild.id] = True
            await message.reply("🎚️ Бассы врубила 💥 — следующий трек жахнет", mention_author=False)
            return True
    # === AI-нейронка для понимания контекста (если не сработал готовый шаблон) ===
    # Понимает "выключи", "поставь на паузу", "включи Ost drone" и т.д. через Muse Spark
    if any(low_clean.startswith(n) for n in names):
        try:
            query = low_clean
            for n in names:
                if query.startswith(n):
                    query = query[len(n):].lstrip(" ,:-")
                    break
            if len(query) < 2:
                return False
            api_key = _cfg("AI_API_KEY", "")
            base_url = _cfg("AI_BASE_URL", "https://opencode.ai/zen/v1/responses")
            if not api_key or "opencode.ai" not in base_url:
                return False
            import aiohttp
            # универсальный классификатор — понимает контекст, настроение, опечатки и различает чат vs музыка, ищет только музыку
            payload = {
                "model": "muse-spark-1.2-contributor-free",
                "instructions": "Ты классификатор для музыкального бота Узи. Пользователь пишет 'Узи <текст>'. Определи intent: PLAY (включить МУЗЫКУ, верни ОПТИМАЛЬНЫЙ YouTube запрос 2-5 слов с упором на музыку), STOP, PAUSE, RESUME, SKIP, QUEUE, JOIN, LEAVE, VOLUME_UP, VOLUME_DOWN, BASS_ON, BASS_OFF, REMOVE (убери трек из очереди), или NO (чат). Для PLAY всегда добавляй музыкальный маркер: OST/music/song/cover чтобы не попалась болтовня. Исправляй опечатки 'что та'->'что-то'. Примеры: 'включи что та из дронов убийц' -> Murder Drones OST music, 'включи что та грустное' -> sad atmospheric music, 'включи дронов убийц форевер' -> Murder Drones Forever OST music, 'дронов убийц' -> Murder Drones OST music, 'убери трек из очереди' -> REMOVE, 'добавь бассов'->BASS_ON, 'еще прибавь'->VOLUME_UP, 'выключи'->STOP, 'как дела?'->NO. ВАЖНО: 'Убийцы вернулись! Неужели 2 сезон' это НЕ музыка — не возвращай такое, только OST/music.",
                "input": query,
                "temperature": 0.4,
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
                    ans = (ans or data.get("output_text") or "").strip().replace('"','').replace("'","").strip()
                    if not ans or ans.upper() == "NO":
                        return False
                    up = ans.upper()
                    # обрабатываем интенты — с проверкой что автор в том же войсе
                    if up == "STOP":
                        if message.guild.voice_client and message.guild.voice_client.is_connected() and not _check_voice_permission(message):
                            await message.reply("🚫 Только в войсе можно выключить!", mention_author=False)
                            return True
                        vc = message.guild.voice_client
                        if vc and (vc.is_playing() or vc.is_paused()):
                            vc.stop()
                            _music_queues[message.guild.id].clear()
                            _now_playing.pop(message.guild.id, None)
                            await message.reply("⏹️ Выключила, как скажешь 💜", mention_author=False)
                        else:
                            await message.reply("Тихо уже, ничего не играет", mention_author=False)
                        return True
                    if up == "PAUSE":
                        if message.guild.voice_client and message.guild.voice_client.is_connected() and not _check_voice_permission(message):
                            await message.reply("🚫 Зайди в мой войс для паузы!", mention_author=False)
                            return True
                        vc = message.guild.voice_client
                        if vc and vc.is_playing():
                            vc.pause()
                            await message.reply("⏸️ Пауза 💜", mention_author=False)
                        else:
                            await message.reply("Нечего ставить на паузу", mention_author=False)
                        return True
                    if up == "RESUME":
                        if message.guild.voice_client and message.guild.voice_client.is_connected() and not _check_voice_permission(message):
                            await message.reply("🚫 Зайди в войс!", mention_author=False)
                            return True
                        vc = message.guild.voice_client
                        if vc and vc.is_paused():
                            vc.resume()
                            await message.reply("▶️ Поехали дальше! 🚁", mention_author=False)
                        else:
                            await message.reply("Нечего продолжать", mention_author=False)
                        return True
                    if up == "SKIP":
                        if message.guild.voice_client and message.guild.voice_client.is_connected() and not _check_voice_permission(message):
                            await message.reply("🚫 Только в войсе можно скипать!", mention_author=False)
                            return True
                        vc = message.guild.voice_client
                        if vc and (vc.is_playing() or vc.is_paused()):
                            vc.stop()
                            await message.reply("⏭️ Скипаю...", mention_author=False)
                        else:
                            await message.reply("Очередь пуста", mention_author=False)
                        return True
                    if up == "QUEUE":
                        q = _music_queues[message.guild.id]
                        now = _now_playing.get(message.guild.id)
                        desc = ""
                        if now: desc += f"▶️ Сейчас: **{now['title']}**\n"
                        if q: desc += "\n".join([f"{i}. {it['title']}" for i, it in enumerate(list(q)[:10], 1)])
                        else: desc += "Очередь пуста."
                        await message.reply(desc or "Тихо...", mention_author=False)
                        return True
                    if up == "JOIN":
                        await _music_join(message, silent=True)
                        return True
                    if up == "LEAVE":
                        if not _check_voice_permission(message) and message.guild.voice_client and message.guild.voice_client.is_connected():
                            await message.reply("🚫 Только в войсе можно выгнать!", mention_author=False)
                            return True
                        await _music_leave(message)
                        return True
                    if up == "VOLUME_DOWN":
                        if message.guild.voice_client and message.guild.voice_client.is_connected() and not _check_voice_permission(message):
                            await message.reply("🚫 Зайди в мой войс чтобы менять громкость!", mention_author=False)
                            return True
                        gid = message.guild.id
                        cur = _music_volume.get(gid, 0.5)
                        new = max(0.0, cur - 0.15)
                        _music_volume[gid] = new
                        vc = message.guild.voice_client
                        if vc and vc.source and hasattr(vc.source, "volume"):
                            try: vc.source.volume = new
                            except: pass
                        await message.reply(f"🔉 Убавила → {int(new*100)}%", mention_author=False)
                        return True
                    if up == "VOLUME_UP":
                        if message.guild.voice_client and message.guild.voice_client.is_connected() and not _check_voice_permission(message):
                            await message.reply("🚫 Зайди в войс чтобы прибавить!", mention_author=False)
                            return True
                        gid = message.guild.id
                        cur = _music_volume.get(gid, 0.5)
                        new = min(2.0, cur + 0.15)
                        _music_volume[gid] = new
                        vc = message.guild.voice_client
                        if vc and vc.source and hasattr(vc.source, "volume"):
                            try: vc.source.volume = new
                            except: pass
                        await message.reply(f"🔊 Прибавила → {int(new*100)}% 💜", mention_author=False)
                        return True
                    if up == "BASS_ON":
                        if message.guild.voice_client and message.guild.voice_client.is_connected() and not _check_voice_permission(message):
                            await message.reply("🚫 Только в войсе басы!", mention_author=False)
                            return True
                        _music_bass[message.guild.id] = True
                        await message.reply("🎚️ Бассы врубила 💥 — следующий трек жахнет", mention_author=False)
                        return True
                    if up == "BASS_OFF":
                        if message.guild.voice_client and message.guild.voice_client.is_connected() and not _check_voice_permission(message):
                            await message.reply("🚫 Зайди в войс!", mention_author=False)
                            return True
                        _music_bass[message.guild.id] = False
                        await message.reply("🎚️ Бассы убрала", mention_author=False)
                        return True
                    if up == "REMOVE":
                        if message.guild.voice_client and message.guild.voice_client.is_connected() and not _check_voice_permission(message):
                            await message.reply("🚫 Только в войсе можно удалять!", mention_author=False)
                            return True
                        q = _music_queues[message.guild.id]
                        if not q:
                            await message.reply("Очередь пуста — нечего убирать", mention_author=False)
                        else:
                            import re as _re
                            m = _re.search(r"\d+", query)
                            if m:
                                idx = int(m.group(0)) - 1
                                ql = list(q)
                                if 0 <= idx < len(ql):
                                    removed = ql.pop(idx)
                                    _music_queues[message.guild.id] = deque(ql, maxlen=_cfg("AI_MAX_HISTORY", 25)*2)
                                    await message.reply(f"🗑️ Убрала: **{removed['title']}**", mention_author=False)
                                else:
                                    removed = q.pop()
                                    await message.reply(f"🗑️ Убрала последний: **{removed['title']}**", mention_author=False)
                            else:
                                removed = q.pop()
                                await message.reply(f"🗑️ Убрала: **{removed['title']}**", mention_author=False)
                        return True
                    # иначе считаем PLAY — остаток это запрос для поиска
                    if message.guild.voice_client and message.guild.voice_client.is_connected() and not _check_voice_permission(message):
                        await message.reply("🚫 Только те кто в войсе со мной могут ставить музыку!", mention_author=False)
                        return True
                    if len(ans) < 2 or len(ans) > 120:
                        if len(ans) > 120: ans = ans[:120]
                        if len(ans) < 2: return False
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
    # TTS: авто-озвучка если автор в войсе с ботом и включено
    if config.TTS_AUTO_VOICE and config.TTS_ENABLED and message.guild and message.guild.voice_client and message.guild.voice_client.is_connected():
        try:
            if message.author.voice and message.author.voice.channel and message.guild.voice_client.channel.id == message.author.voice.channel.id:
                # не озвучиваем ошибки AI
                if not answer.startswith("❌") and not answer.startswith("⏳"):
                    bot.loop.create_task(_tts_speak(message.guild, answer, message.channel.id))
        except Exception as e:
            print(f"tts auto fail: {e}")

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

@bot.tree.command(name="скажи", description="Узи озвучит текст в войсе (TTS)")
@app_commands.describe(text="Что сказать (до 300 символов)", voice="Голос")
@app_commands.choices(voice=[
    app_commands.Choice(name="Узи (Svetlana)", value="ru-RU-SvetlanaNeural"),
    app_commands.Choice(name="Дмитрий", value="ru-RU-DmitryNeural"),
    app_commands.Choice(name="Aria (EN)", value="en-US-AriaNeural"),
])
async def slash_say(interaction: discord.Interaction, text: str, voice: str = None):
    if not config.TTS_ENABLED:
        await interaction.response.send_message("🔇 TTS выключен", ephemeral=True)
        return
    if len(text) > 350:
        text = text[:350]
    if interaction.user.voice and interaction.user.voice.channel:
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            try:
                await interaction.user.voice.channel.connect()
            except Exception as e:
                await interaction.response.send_message(f"❌ Не зашла в войс: {e}", ephemeral=True)
                return
    else:
        await interaction.response.send_message("Зайди в войс сначала! 🚁", ephemeral=True)
        return
    if voice:
        # временно подменяем голос
        old = config.TTS_VOICE
        config.TTS_VOICE = voice
        # не меняем env, только runtime
    await interaction.response.send_message(f"🗣️ Узи говорит: *{text[:120]}*")
    try:
        await _tts_speak(interaction.guild, text, interaction.channel.id)
    finally:
        if voice:
            config.TTS_VOICE = old

@bot.tree.command(name="tts", description="Вкл/выкл авто-озвучку AI (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def slash_tts_toggle(interaction: discord.Interaction, enabled: bool):
    config.TTS_AUTO_VOICE = enabled
    config.TTS_ENABLED = enabled
    os.environ["TTS_ENABLED"] = "true" if enabled else "false"
    os.environ["TTS_AUTO_VOICE"] = "true" if enabled else "false"
    await interaction.response.send_message(f"✅ TTS {'включен' if enabled else 'выключен'} (авто-озвучка {'вкл' if enabled else 'выкл'})", ephemeral=True)

@bot.tree.command(name="слушай", description="Включить распознавание голоса (STT) на N секунд")
@app_commands.describe(seconds="Сколько секунд слушать (5-60)")
async def slash_listen(interaction: discord.Interaction, seconds: int = 15):
    if not 5 <= seconds <= 60:
        await interaction.response.send_message("5-60 сек", ephemeral=True)
        return
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("Зайди в войс! 🚁", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        try:
            await interaction.user.voice.channel.connect()
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
    ok, msg = await _stt_start(interaction.guild, interaction.channel.id, seconds)
    await interaction.response.send_message(msg, ephemeral=not ok)

@bot.tree.command(name="стоп-слушай", description="Остановить прослушку")
async def slash_stop_listen(interaction: discord.Interaction):
    await _stt_stop(interaction.guild)
    await interaction.response.send_message("🛑 Стоп слушаю", ephemeral=True)

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
@slash_say.error
@slash_tts_toggle.error
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
                try:
                    from waitress import serve
                    serve(panel_app, host="0.0.0.0", port=port, threads=4)
                except ImportError:
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

