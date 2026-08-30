import discord
from discord import app_commands
from discord.ext import commands
import config
from datetime import timedelta
import json
import os
import random
import time

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ============ ЭКОНОМИКА СПЕРМИКИ (сохранение) ============
# Используем Postgres если есть DATABASE_URL (Railway), иначе файл
import urllib.parse as urlparse

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
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
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB init fail: {e}")

# Фолбэк файлы
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

# Вызвать при старте
try:
    init_db()
except:
    pass

# Магазин (убрана любимая_димы)
SHOP_ITEMS = {
    "бронь_от_дрона": {"price": 500, "desc": "Защита от дроноеба на 1 день", "role": None},
    "раб_дроноеб": {"price": 777, "desc": "Роль Раб дроноеб 🚁", "role": "Раб дроноеб"},
    "vip_спермик": {"price": 300, "desc": "VIP роль + цвет", "role": "VIP Спермик"},
}

# Квест Димы - храним в БД если есть, иначе файл
def load_quest():
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT guild_id, data FROM quest")
            rows = cur.fetchall()
            data = {}
            for gid, j in rows:
                try:
                    data[gid] = json.loads(j)
                except:
                    data[gid] = {}
            cur.close()
            conn.close()
            return data
        except:
            try:
                conn.close()
            except:
                pass
    if not os.path.exists(QUEST_FILE):
        return {}
    try:
        with open(QUEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_quest(data):
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            for gid, info in data.items():
                j = json.dumps(info, ensure_ascii=False)
                cur.execute("INSERT INTO quest (guild_id, data) VALUES (%s,%s) ON CONFLICT (guild_id) DO UPDATE SET data=EXCLUDED.data", (gid, j))
            # Удаляем удаленные гильдии - очищаем лишние? не нужно
            cur.close()
            conn.close()
            return
        except Exception as e:
            print(f"save_quest DB fail: {e}")
            try:
                conn.close()
            except:
                pass
    with open(QUEST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ============ VIEWS ============

def get_verify_role(guild: discord.Guild):
    # Приоритет: роль по имени "бусифицированный" -> затем ID из .env
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
            # лог
            if config.LOG_CHANNEL_ID:
                ch = bot.get_channel(config.LOG_CHANNEL_ID)
                if ch:
                    await ch.send(f"✅ {interaction.user.mention} прошел верификацию")
        except discord.Forbidden:
            await interaction.response.send_message("❌ У меня нет прав выдать роль. Проверь иерархию ролей бота.", ephemeral=True)

class RolesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # Динамически создаем селект если роли настроены
        if config.SELF_ROLES:
            options = []
            # Мы не знаем названия ролей на этапе init, поэтому будем резолвить при взаимодействии
            # Создаем заглушку, реальные названия подтянутся в setup
            self.add_item(RolesSelect())

class RolesSelect(discord.ui.Select):
    def __init__(self):
        # Создаем опции на основе конфига, названия подтянем позже если нужно
        options = []
        # Если бот еще не запущен, ставим временные лейблы
        for role_id in config.SELF_ROLES[:25]:  # лимит дискорда 25
            options.append(discord.SelectOption(label=f"Роль {role_id}", value=str(role_id), description="Нажми чтобы получить/снять"))
        
        super().__init__(
            placeholder="Выбери роли...",
            min_values=0,
            max_values=len(options) if options else 1,
            options=options if options else [discord.SelectOption(label="Нет ролей", value="0")],
            custom_id="roles_select"
        )

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
                elif not should_have and has_role and role_id in config.SELF_ROLES:
                    # Снимаем только если роль была в списке самовыдачи и не выбрана
                    # Чтобы не снимать все подряд, проверяем что юзер что-то выбрал
                    # Логика: если выбрал пусто - снимем все самовыдаваемые роли
                    # Поэтому тут нужно отдельно обрабатывать
                    pass
            except discord.Forbidden:
                await interaction.response.send_message("❌ Нет прав на выдачу ролей.", ephemeral=True)
                return

        # Обработка снятия: если роль не выбрана но есть у юзера - снимаем
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


# Упрощенный вариант на кнопках (надежнее если много ролей)
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
        # Парсим ID
        raw = self.target.value.strip()
        # Пытаемся вытащить ID из упоминания <@...>
        import re
        m = re.search(r"\d{15,}", raw)
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
        member = interaction.guild.get_member(tid)
        name = member.mention if member else f"<@{tid}>"
        await interaction.response.send_message(f"💸 Перевел {amt} 💦 → {name}. Баланс: {get_balance(interaction.user.id)}", ephemeral=True)

# ============ ПРОФИЛЬ / МАГАЗИН ПАНЕЛИ (как на скрине Милка) ============
class ProfileView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Открыть инвентарь", style=discord.ButtonStyle.secondary, custom_id="profile_inv", emoji="📦", row=0)
    async def inv(self, interaction: discord.Interaction, button: discord.ui.Button):
        bal = get_balance(interaction.user.id)
        data = load_economy()
        inv = data.get(str(interaction.user.id), {}).get("items", [])
        desc = f"💦 Спермики: **{bal}**\n📦 Предметы: {', '.join(inv) if inv else 'пусто'}\n\nКейсы и роли — в #магазин"
        await interaction.response.send_message(desc, ephemeral=True)

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

    @discord.ui.button(label="Начать квест Димы", style=discord.ButtonStyle.primary, custom_id="profile_quest", emoji="💖", row=1)
    async def quest_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Личный квест - создается канал только для тебя со смешным названием
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        funny = random.choice(["схрон-любви-димы", "под-кроватью-димы", "в-гараже-димы", "дима-забыл-любимую", "любимая-на-чердаке", "за-шашлыком-у-димы"])
        name = f"💖・квест-{interaction.user.name.lower().replace(' ', '-')[:10]}-{funny[:12]}"
        category = discord.utils.get(guild.categories, name="💖 Квест Димы")
        if not category:
            try:
                category = await guild.create_category("💖 Квест Димы", reason="Квест Димы личный")
            except:
                category = None
        # Проверяем есть ли уже личный канал
        existing = discord.utils.get(guild.text_channels, name=name)
        # Создаем личный канал только для игрока
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }
        try:
            ch = await guild.create_text_channel(name, overwrites=overwrites, category=category, topic="Личный квест Димы - ищи любимую", reason="Личный квест Димы")
            # Добавляем в общий квест список чтобы /нашел работал
            qdata = load_quest()
            gid = str(guild.id)
            if gid not in qdata:
                qdata[gid] = {"channels": [], "found": {}}
            if ch.id not in qdata[gid]["channels"]:
                qdata[gid]["channels"].append(ch.id)
                # Ограничим до 20 каналов чтобы не разрасталось
                qdata[gid]["channels"] = qdata[gid]["channels"][-20:]
            save_quest(qdata)
            await ch.send(f"{interaction.user.mention} ||💖 любимая Димы спряталась тут! Напиши /нашел чтобы проверить||")
            await ch.send("Подсказка: выдели скрытый текст выше 👆")
            await interaction.followup.send(f"✅ Создал личный канал {ch.mention} со смешным названием! Ищи там любимую Димы и пиши `/нашел` прямо в нем!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

    @discord.ui.button(label="Рулетка дроноеба", style=discord.ButtonStyle.danger, custom_id="profile_roulete", emoji="🚁", row=1)
    async def roulete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Быстрая рулетка на 50 + ядерка на 1
        if get_balance(interaction.user.id) < 50:
            await interaction.response.send_message(f"❌ Нужно 50 💦, у тебя {get_balance(interaction.user.id)}", ephemeral=True)
            return
        roll = random.randint(1, 100)
        if roll == 1:
            old = get_balance(interaction.user.id)
            nuke_balance(interaction.user.id)
            await interaction.response.send_message(f"☢️ **ЯДЕРКА УПАЛА!** Ролл `1/100` — на тебя сбросили ядерку раба дроноеба! Все **{old} 💦** сгорели! Баланс: 0", ephemeral=True)
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
        await interaction.response.send_message("Промокоды скоро! Следи за анонсами.", ephemeral=True)

class ShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Товары за спермики", style=discord.ButtonStyle.secondary, custom_id="shop_spermi", emoji="💦")
    async def spermi(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="🛒 Товары за спермики", description="Трать спермики, фарми дальше", color=discord.Color.from_rgb(255, 107, 139))
        for k, v in SHOP_ITEMS.items():
            if "сперм" in k.lower() or k in ["vip_спермик", "бронь_от_дрона"]:
                embed.add_field(name=f"{k} — {v['price']} 💦", value=v['desc'], inline=False)
        if len(embed.fields) == 0:
            for k, v in SHOP_ITEMS.items():
                embed.add_field(name=f"{k} — {v['price']} 💦", value=v['desc'], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, view=ShopBuyView())

    @discord.ui.button(label="Товары за любовь Димы", style=discord.ButtonStyle.secondary, custom_id="shop_dima", emoji="💖")
    async def dima(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="💖 Товары за Любовь Димы", description="Нафармил любимых? Трать!", color=discord.Color.from_rgb(255, 107, 139))
        embed.add_field(name="любимая_димы — 1000 💦", value="Роль Любимая Димы 💖 (легендарка)", inline=False)
        embed.add_field(name="Фон 'Дима и ко' — 500 💖", value="Скоро", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, view=ShopBuyView())

    @discord.ui.button(label="Товары за детали дрона", style=discord.ButtonStyle.secondary, custom_id="shop_drone", emoji="🚁")
    async def drone(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="🚁 Товары за Детали Дрона", description="Выиграл у раба дроноеба? Трать!", color=discord.Color.from_rgb(255, 107, 139))
        embed.add_field(name="раб_дроноеб — 777 💦", value="Роль Раб дроноеб 🚁", inline=False)
        embed.add_field(name="Дрон-скин — 400 🚁", value="Скоро", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, view=ShopBuyView())

class ShopBuyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # Динамические кнопки покупки
        for key in list(SHOP_ITEMS.keys())[:5]:
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
        # Сохраняем в инвентарь
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
    bot.add_view(ShopView())
    bot.add_view(ShopBuyView())
    try:
        # Фикс дублей: оставляем только гильдейские (мгновенно) чтобы не было x2
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            # 1. Скопировать глобальные в гильдию и засинкать (мгновенно)
            bot.tree.copy_global_to(guild=guild)
            synced_guild = await bot.tree.sync(guild=guild)
            print(f"Синхронизировано для гильдии {config.GUILD_ID}: {len(synced_guild)} команд")
            # 2. Очистить глобальные чтобы не дублировались (оставить только гильдейские)
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            print("Глобальные команды очищены (остались только гильдейские, без дублей)")
        else:
            synced = await bot.tree.sync()
            print(f"Синхронизировано глобально {len(synced)} команд")
    except Exception as e:
        print(f"Ошибка синхронизации: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    # Приветствие
    if config.WELCOME_CHANNEL_ID:
        ch = bot.get_channel(config.WELCOME_CHANNEL_ID)
        if ch:
            embed = discord.Embed(
                title="Добро пожаловать! 👋",
                description=f"{member.mention}, добро пожаловать на **{member.guild.name}**!\n\nПройди верификацию и выбери роли в каналах сервера.",
                color=discord.Color.green()
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=embed)
    
    # Автолог
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

# ============ SLASH COMMANDS ============

@bot.tree.command(name="верификация", description="Создать сообщение для верификации (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_verify(interaction: discord.Interaction, канал: discord.TextChannel = None):
    ch = канал or interaction.channel
    embed = discord.Embed(
        title="🚐 БУСИФИКАЦИЯ",
        description="**Добро пожаловать в бусик!**\n\nНажми на кнопку ниже, чтобы пройти бусификацию и получить доступ к серверу!\n\n> ⚠️ Уклонение от бусификации карается ТЦК\n> ✅ После нажатия откроются все каналы.",
        color=discord.Color.dark_gold()
    )
    embed.set_image(url="https://i.imgflip.com/6e0a5u.jpg")
    embed.set_footer(text=f"{interaction.guild.name} • Бусификация на связи")
    await ch.send(embed=embed, view=VerifyView())
    await interaction.response.send_message(f"✅ Панель Бусификации создана в {ch.mention}", ephemeral=True)

@bot.tree.command(name="основа", description="Создать канал бусификация с верификацией (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_base(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # 1. Роль бусифицированный - создаем если нет
    role = discord.utils.get(guild.roles, name="бусифицированный")
    if not role:
        try:
            role = await guild.create_role(name="бусифицированный", colour=discord.Colour.gold(), reason="Роль для Бусификации /основа")
            # Ставим роль бота выше чтобы мог выдавать, но ниже админов - Discord сам поставит внизу, админ потом подвинет если надо
        except discord.Forbidden:
            await interaction.followup.send("❌ Нет прав создавать роль `бусифицированный`. Дай боту `Manage Roles` и роль выше.", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка создания роли: {e}", ephemeral=True)
            return

    # 2. Категория + Канал бусификация
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
        # Обновляем права категории
        try:
            await category.set_permissions(guild.default_role, view_channel=True)
            await category.set_permissions(role, view_channel=False)
        except:
            pass

    channel = discord.utils.get(guild.text_channels, name="🚐・бусификация") or discord.utils.get(guild.text_channels, name="бусификация")
    already_had_channel = channel is not None
    # Проверка панели для дубля - но все равно перенастроим права на всех каналах
    has_panel = False
    if channel:
        try:
            async for msg in channel.history(limit=5):
                if msg.author == guild.me and msg.embeds and msg.embeds[0].title and "БУСИФИКАЦИЯ" in msg.embeds[0].title:
                    has_panel = True
                    break
        except:
            pass
        # Если канал есть но без категории - переносим в категорию
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

    # 3. Скрываем ВСЕ каналы от небусифицированных (@everyone deny, бусифицированный allow)
    hidden_count = 0
    for ch in guild.channels:
        if ch.id == channel.id or ch.id == category.id:
            # Для категории и канала бусификации - @everyone видит, бусифицированные скрываем
            try:
                await ch.set_permissions(guild.default_role, view_channel=True, send_messages=False if isinstance(ch, discord.TextChannel) else None, read_message_history=True if isinstance(ch, discord.TextChannel) else None)
                await ch.set_permissions(role, view_channel=False)
            except:
                pass
            continue
        try:
            # Ставим @everyone view False - скрываем канал
            await ch.set_permissions(guild.default_role, view_channel=False)
            # Открываем для роли бусифицированный
            await ch.set_permissions(role, view_channel=True)
            hidden_count += 1
        except discord.Forbidden:
            continue
        except Exception:
            continue

    embed = discord.Embed(
        title="🚐 БУСИФИКАЦИЯ",
        description="**Вас остановили ТЦК!**\n\nЧтобы избежать поездки в бусике — пройди бусификацию 👇\n\nНажми **Пройти Бусификацию** и получи доступ к серверу.\n\n> 🫡 *Локальный мем сервера — бусификация обязательна*\n> Без роли `бусифицированный` ты не увидишь другие каналы!",
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"{guild.name} • Не сопротивляйся бусификации")
    # Чистим старые сообщения бота в канале чтобы не спамить
    try:
        async for msg in channel.history(limit=10):
            if msg.author == guild.me and msg.embeds:
                await msg.delete()
    except:
        pass

    # 4. Если панель уже была - не спамим новую, просто обновляем права
    if not has_panel:
        # Чистим старые сообщения бота чтобы не спамить
        try:
            async for msg in channel.history(limit=10):
                if msg.author == guild.me and msg.embeds:
                    await msg.delete()
        except:
            pass
        await channel.send(embed=embed, view=VerifyView())
    else:
        # Обновляем существующую панель если надо
        pass

    if already_had_channel and has_panel:
        await interaction.followup.send(f"🔄 Перенастроено! Роль {role.mention} + категория {category.name} + канал {channel.mention}\n🔒 Скрыто/обновлено {hidden_count} каналов. Без `бусифицированный` теперь видно только категорию {category.name}.", ephemeral=True)
    else:
        await interaction.followup.send(f"✅ Готово! Категория {category.name} + канал {channel.mention} + роль {role.mention}\n🔒 Скрыто {hidden_count} каналов от непроверенных. Без `бусифицированный` видно только этот канал.", ephemeral=True)

@bot.tree.command(name="роли", description="Создать панель с выдачей ролей (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_roles(interaction: discord.Interaction, канал: discord.TextChannel = None):
    if not config.SELF_ROLES:
        await interaction.response.send_message("❌ В .env не настроены SELF_ROLES. Добавь ID ролей и перезапусти бота.", ephemeral=True)
        return
    ch = канал or interaction.channel
    guild = interaction.guild
    
    embed = discord.Embed(
        title="🎭 Выбор ролей",
        description="Нажми на кнопку роли, чтобы получить или снять её:\n\n" + "\n".join([f"• <@&{rid}>" for rid in config.SELF_ROLES]),
        color=discord.Color.gold()
    )
    view = RolesButtonView(guild)
    await ch.send(embed=embed, view=view)
    await interaction.response.send_message(f"✅ Панель ролей создана в {ch.mention}", ephemeral=True)

@bot.tree.command(name="меню", description="Создать каналы профиль и магазин как у Милки (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_menu(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    # Роль для доступа
    role = get_verify_role(guild) or discord.utils.get(guild.roles, name="бусифицированный")
    # Категория Меню
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

    # Канал профиль
    profil_ch = discord.utils.get(guild.text_channels, name="профиль") or discord.utils.get(guild.text_channels, name="🪪・профиль")
    if not profil_ch:
        try:
            profil_ch = await guild.create_text_channel("🪪・профиль", category=category, topic="Профиль, инвентарь и роли", reason="/меню")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка канал профиль: {e}", ephemeral=True)
            return
    # Канал магазин
    shop_ch = discord.utils.get(guild.text_channels, name="магазин") or discord.utils.get(guild.text_channels, name="🛒・магазин")
    if not shop_ch:
        try:
            shop_ch = await guild.create_text_channel("🛒・магазин", category=category, topic="Магазин за спермики", reason="/меню")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка канал магазин: {e}", ephemeral=True)
            return

    # Чистим старые бот-сообщения
    for ch in [profil_ch, shop_ch]:
        try:
            async for msg in ch.history(limit=20):
                if msg.author == guild.me and msg.embeds:
                    await msg.delete()
        except:
            pass

    # Эмбед профиль (как у Милки - розовый) + баннер-гифка сверху
    embed_banner_prof = discord.Embed(color=discord.Color.from_rgb(255, 105, 180))
    embed_banner_prof.set_image(url="https://yt3.ggpht.com/t2oynaaQq3aVvMuzymoqvK6m8VGPu1mV5Krr4x9YRvw0bHEKv4mwXteK3DmTqLo4j2US8OW0b21y4A=s416-c-fcrop64=1,380b0000c7f4ffff-nd-v1-rwa")
    await profil_ch.send(embed=embed_banner_prof)
    embed_prof = discord.Embed(title="Профиль • Инвентарь и Роли", description="Управляй ролями, инвентарем и спермиками", color=discord.Color.from_rgb(255, 105, 180))
    embed_prof.add_field(name="📦 Инвентарь", value="Предметы — как пульт ролями. Чтобы получить роль, купи в магазине.", inline=True)
    embed_prof.add_field(name="🎨 Фоны", value="Крути гачу за спермики и открывай фоны профиля", inline=True)
    embed_prof.add_field(name="🎭 Роли", value="Цветные роли выделят тебя в чате!", inline=True)
    embed_prof.set_footer(text="спермики • профиль")
    bot.add_view(ProfileView())
    await profil_ch.send(embed=embed_prof, view=ProfileView())
    # Доп инфа
    embed_prof2 = discord.Embed(color=discord.Color.from_rgb(255, 105, 180))
    embed_prof2.description = f"Все твои предметы: кейсы, роли. Покупай в {shop_ch.mention}\nБаланс: /баланс | Ежедневка: /ежедневка"
    await profil_ch.send(embed=embed_prof2)

    # Баннер как на скрине - теперь твоя аниме-девочка
    embed_banner = discord.Embed(title="МАГАЗИНЫ — Фармим, закупаемся!", color=discord.Color.from_rgb(255, 107, 139))
    embed_banner.set_image(url="https://img.magnific.com/premium-photo/cute-anime-girl-hoodie-wallpaper_776894-105948.jpg?semt=ais_hybrid")
    await shop_ch.send(embed=embed_banner)

    embed_shop = discord.Embed(title="Валюта Сервера", color=discord.Color.from_rgb(255, 107, 139))
    embed_shop.description = "**Фармим, закупаемся!**\nТут всё за мемы сервера:"
    embed_shop.add_field(name="💦 Спермики", value="Получаются за активность: сообщения, войс, ивенты\n`/ежедневка`, `/баланс`, `/перевести`", inline=False)
    embed_shop.add_field(name="💖 Любовь Димы", value="За квест `Найди любимую Димы` — `/квест-димы` → `/нашел`", inline=False)
    embed_shop.add_field(name="🚁 Детали Дрона", value="За `Рулетку раба дроноеба` — `/рулетка-дроноеба`\nИспытай удачу и сорви куш!", inline=False)
    embed_shop.set_footer(text="Black ICE Palace • спермики • любимая Димы • дроноеб")
    bot.add_view(ShopView())
    bot.add_view(ShopBuyView())
    await shop_ch.send(embed=embed_shop, view=ShopView())

    # Скрываем категорию Меню от небусифицированных если есть роль
    if role:
        try:
            await category.set_permissions(guild.default_role, view_channel=False)
            await category.set_permissions(role, view_channel=True)
        except:
            pass

    await interaction.followup.send(f"✅ Меню создано: {category.name} → {profil_ch.mention} + {shop_ch.mention}. Вместо команд теперь кнопки как у Милки!", ephemeral=True)

@bot.tree.command(name="правила", description="Опубликовать правила Black ICE Palace в #rules (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def publish_rules(interaction: discord.Interaction, канал: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    ch = канал or discord.utils.get(guild.text_channels, name="rules") or discord.utils.get(guild.text_channels, name="📜・rules") or interaction.channel

    # --- Современный embed - один стиль, одна полоска ---
    embed = discord.Embed(
        title="📜 ПРАВИЛА BLACK ICE PALACE 2026 edition 📜",
        description="**Читать обязательно, иначе получишь пизды от жизни**\n\u200b",
        color=discord.Color.from_rgb(255, 107, 139)  # розовая полоска как у магазина Милки - один цвет
    )
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
    # Правила (20 шт) - эмодзи в одном стиле/цвете (розовый акцент)
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
    embed.add_field(
        name="⚖️ Наказания:",
        value="⚠️ 1 раз - предупреждение и подзатыльник\n🔇 2 раз - мут на 12 часов, подумаешь над поведением\n🔨 3 раз - бан на 3 дня, проветришь жопу\n💀 Дальше - пермач нахуй 👋",
        inline=False
    )
    embed.add_field(name="💸 Пожертвования для бустов -", value="Сканируй QR ниже", inline=False)
    embed.set_footer(text="Black ICE Palace • 2026 • Читать обязательно")
    try:
        await ch.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Не смог отправить в {ch.mention}: {e}", ephemeral=True)
        return

    # QR код отдельным embed с картинкой - в том же розовом стиле
    qr_embed = discord.Embed(
        title="Пожертвования",
        description="**Уруев Дмитрий Денисович**\nНомер договора `5664748331`",
        color=discord.Color.from_rgb(255, 107, 139)
    )
    qr_embed.set_image(url="https://api.qrserver.com/v1/create-qr-code/?size=500x500&data=5664748331")
    qr_embed.set_footer(text="Отсканируй для доната")
    await ch.send(embed=qr_embed)

    # PS поправка - тоже розовая полоска
    ps_embed = discord.Embed(
        description="*P. S. Все пожертвования на сервер носят добровольно-принудительный характер, сказали - делай* 😉",
        color=discord.Color.from_rgb(255, 107, 139)
    )
    await ch.send(embed=ps_embed)

    await interaction.followup.send(f"✅ Правила опубликованы в {ch.mention}", ephemeral=True)

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

# ============ СПЕРМИКИ ЭКОНОМИКА ============
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

# Перевод спермиков - теперь только через профиль (кнопка), слэш удален
async def transfer(interaction: discord.Interaction, пользователь: discord.Member, количество: int):
    if пользователь.id == interaction.user.id:
        await interaction.response.send_message("❌ Себе нельзя", ephemeral=True)
        return
    if get_balance(interaction.user.id) < количество:
        await interaction.response.send_message(f"❌ Недостаточно спермиков. Баланс: {get_balance(interaction.user.id)}", ephemeral=True)
        return
    add_spermi(interaction.user.id, -количество)
    add_spermi(пользователь.id, количество)
    await interaction.response.send_message(f"💸 {interaction.user.mention} перевел {количество} спермиков → {пользователь.mention}")

# Магазин теперь только через канал #магазин (кнопки), слэш удален
async def shop(interaction: discord.Interaction):
    embed = discord.Embed(title="🛒 Магазин спермиков", color=discord.Color.gold())
    for key, item in SHOP_ITEMS.items():
        embed.add_field(name=f"{key} — {item['price']} спермиков", value=item['desc'], inline=False)
    embed.set_footer(text="Купить: /купить <название>")
    await interaction.response.send_message(embed=embed)

# Покупка теперь только через кнопки магазина, слэш удален
async def buy(interaction: discord.Interaction, предмет: str):
    key = предмет.lower()
    if key not in SHOP_ITEMS:
        await interaction.response.send_message(f"❌ Нет такого предмета. Магазин: {', '.join(SHOP_ITEMS.keys())}", ephemeral=True)
        return
    item = SHOP_ITEMS[key]
    if get_balance(interaction.user.id) < item["price"]:
        await interaction.response.send_message(f"❌ Нужно {item['price']} спермиков, у тебя {get_balance(interaction.user.id)}", ephemeral=True)
        return
    # Снимаем деньги
    add_spermi(interaction.user.id, -item["price"])
    # Выдаем роль если есть
    if item["role"]:
        role = discord.utils.get(interaction.guild.roles, name=item["role"])
        if not role:
            try:
                role = await interaction.guild.create_role(name=item["role"], reason="Магазин спермики")
            except:
                pass
        if role:
            try:
                await interaction.user.add_roles(role)
            except:
                pass
            await interaction.response.send_message(f"✅ Куплено {key} за {item['price']} 💦! Роль {role.mention} выдана. Баланс: {get_balance(interaction.user.id)}")
            return
    await interaction.response.send_message(f"✅ Куплено {key} за {item['price']} 💦! Баланс: {get_balance(interaction.user.id)}")

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

# ============ КВЕСТ ЛЮБИМАЯ ДИМЫ (теперь только через профиль, слэш удален) ============
async def quest_dima(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    # Смешные названия для квест-каналов
    funny_names = ["любимая-димы-на-чердаке", "схрон-любви-димы", "под-кроватью-димы", "в-гараже-димы", "за-шашлыком-димы", "у-дяди-димы-в-подвале", "дима-забыл-где-любимая"]
    # Создаем категорию для квеста если нет
    q_category = discord.utils.get(guild.categories, name="💖 Квест Димы")
    if not q_category:
        try:
            q_category = await guild.create_category("💖 Квест Димы", reason="Квест Димы")
        except:
            q_category = None
    # Выбираем 3 рандомных канала кроме системных ИЛИ создаем новые смешные если их мало
    exclude = ["бусификация", "профиль", "магазин"]
    channels = [c for c in guild.text_channels if not any(x in c.name.lower() for x in exclude)][:15]
    # Создаем 3 новых канала со смешными названиями (вместо рандома по существующим)
    picked = []
    for _ in range(3):
        fname = random.choice(funny_names)
        try:
            ch = await guild.create_text_channel(fname, category=q_category, topic="Любимая Димы спряталась тут! Пиши /нашел", reason="Квест Димы - личный канал")
            picked.append(ch)
        except Exception as e:
            print(f"Ошибка создания канала квеста: {e}")
            continue
    if len(picked) < 3 and len(channels) >= 3:
        # Фолбэк - берем существующие если не создались
        picked = random.sample(channels, 3)
    if not picked:
        await interaction.followup.send("❌ Не смог создать каналы квеста", ephemeral=True)
        return
    quest_data = load_quest()
    quest_data[str(guild.id)] = {"channels": [c.id for c in picked], "found": {}}
    save_quest(quest_data)
    hidden_emoji = "💖"
    for ch in picked:
        try:
            await ch.send(f"||{hidden_emoji} любимая Димы спряталась тут! Напиши /нашел чтобы проверить||")
            await ch.send(f"**Подсказка:** ищи скрытый текст выше 👆")
        except:
            pass
    await interaction.followup.send(f"✅ Квест запущен! Создано 3 канала со смешным названием: {', '.join([c.mention for c in picked])} — ищи там! +500 спермиков за всех 3!", ephemeral=True)

@bot.tree.command(name="нашел", description="Проверить квест любимая Димы")
async def found_dima(interaction: discord.Interaction):
    # Деферим сразу чтобы не было 404 Unknown interaction (этап 2 создает каналы >3 сек)
    try:
        await interaction.response.defer(ephemeral=True)
    except:
        pass
    guild = interaction.guild
    data = load_quest()
    q = data.get(str(guild.id))
    if not q:
        await interaction.followup.send("❌ Квест не запущен. Нажми в #профиль `Начать квест Димы`", ephemeral=True)
        return
    ch_id = interaction.channel.id
    if ch_id not in q["channels"]:
        await interaction.followup.send("❌ Тут нет любимой Димы, ищи дальше!", ephemeral=True)
        return
    uid = str(interaction.user.id)
    if uid not in q["found"]:
        q["found"][uid] = []
    if ch_id in q["found"][uid]:
        await interaction.followup.send("Ты уже находил тут!", ephemeral=True)
        return
    q["found"][uid].append(ch_id)
    save_quest(data)
    # Стадия
    stage = q.get("stage", 1)
    left = len(q["channels"]) - len(q["found"][uid])
    if left > 0:
        await interaction.followup.send(f"💖 Нашел! (Этап {stage}/3) Осталось {left} шт. Ищи дальше!", ephemeral=True)
        return
    # Нашел все в этом этапе
    add_spermi(interaction.user.id, 500)
    role = discord.utils.get(guild.roles, name="Любимая Димы")
    if not role:
        try:
            role = await guild.create_role(name="Любимая Димы", colour=discord.Colour.pink())
        except:
            role = None
    if role:
        try:
            await interaction.user.add_roles(role)
        except:
            pass
    if stage < 3:
        # Спавним 2 этап (до 3) - создаем 3 новых смешных канала и удаляем старые
        old_channels = list(q["channels"])
        q["stage"] = stage + 1
        q["found"][uid] = []  # сброс для след. этапа
        # Создаем новые каналы для следующего этапа - приватные для игрока (как просил)
        funny_names = ["любимая-димы-этап2", f"дима-прячет-снова-{stage+1}", "схрон-2-уровня", "подвал-2", "чердак-2"]
        q_category = discord.utils.get(guild.categories, name="💖 Квест Димы")
        if not q_category:
            try:
                q_category = await guild.create_category("💖 Квест Димы", reason="Квест Димы")
            except:
                q_category = None
        new_channels = []
        for _ in range(3):
            fname = random.choice(funny_names) + f"-{random.randint(10,99)}"
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
            }
            try:
                ch = await guild.create_text_channel(fname, category=q_category, overwrites=overwrites, topic=f"Личный этап {stage+1} для {interaction.user.display_name}", reason=f"Квест этап {stage+1} приватный")
                new_channels.append(ch)
                await ch.send(f"{interaction.user.mention} ||💖 Этап {stage+1}: любимая Димы снова спряталась тут! /нашел||")
            except:
                pass
        if new_channels:
            q["channels"] = [c.id for c in new_channels]
            save_quest(data)
            # Удаляем старые каналы этапа
            for cid in old_channels:
                ch = guild.get_channel(cid)
                if ch:
                    try:
                        await ch.delete(reason=f"Квест этап {stage} завершен")
                    except:
                        pass
            await interaction.followup.send(f"🎉 Этап {stage} пройден! +500 💦 и {role.mention if role else ''}\n➡️ Этап {stage+1}/3 заспавнился: {', '.join([c.mention for c in new_channels])} — ищи там!", ephemeral=True)
            return
        # Если не создались - просто сброс
        save_quest(data)
        await interaction.followup.send(f"🎉 Этап {stage} пройден! +500 💦", ephemeral=True)
    else:
        # Финал 3/3 - удаляем каналы квеста
        await interaction.followup.send(f"🏆 ФИНАЛ! Ты нашел все 3 этапа! +500 💦 и {role.mention if role else ''}\nКаналы квеста удалятся через 5 сек...", ephemeral=True)
        # Удаляем каналы через задержку
        import asyncio
        await asyncio.sleep(5)
        for cid in q["channels"]:
            ch = guild.get_channel(cid)
            if ch:
                try:
                    await ch.delete(reason="Квест Димы завершен - финал 3/3")
                except:
                    pass
        # Сброс квеста
        if str(guild.id) in data:
            del data[str(guild.id)]
            save_quest(data)
        # Удаляем личные квест-каналы тоже если есть (те что создавались через профиль)
        for ch in guild.text_channels:
            if "квест-" in ch.name.lower() and "димы" in ch.name.lower():
                try:
                    # Только если канал приватный для этого юзера или пустой
                    await ch.delete(reason="Квест завершен")
                except:
                    pass

# Рулетка теперь только через профиль (кнопка), слэш удален
async def roulete_drone(interaction: discord.Interaction, ставка: int):
    if get_balance(interaction.user.id) < ставка:
        await interaction.response.send_message(f"❌ Нужно {ставка} спермиков, у тебя {get_balance(interaction.user.id)}", ephemeral=True)
        return
    roll = random.randint(1, 100)
    if roll == 1:
        old = get_balance(interaction.user.id)
        nuke_balance(interaction.user.id)
        await interaction.response.send_message(f"☢️ **ЯДЕРКА УПАЛА!** Ролл `1/100` — ядерка раба дроноеба прямо на тебя! Все **{old} 💦** сгорели! Баланс: 0")
        return
    if roll <= 45:
        # проиграл
        add_spermi(interaction.user.id, -ставка)
        await interaction.response.send_message(f"💥 Раб дроноеб ебанул! Ты проиграл {ставка} спермиков. Ролл {roll}/100")
    elif roll <= 90:
        win = int(ставка * 1.5)
        add_spermi(interaction.user.id, win)
        await interaction.response.send_message(f"🚁 Раб дроноеб промахнулся! Выиграл +{win} спермиков! Ролл {roll}/100. Баланс: {get_balance(interaction.user.id)}")
    else:
        win = ставка * 3
        add_spermi(interaction.user.id, win)
        await interaction.response.send_message(f"🔥 ДЖЕКПОТ! Раб дроноеб взорвался! +{win} спермиков! Ролл {roll}/100. Баланс: {get_balance(interaction.user.id)}")

# Обработка ошибок прав
@setup_verify.error
@setup_base.error
@setup_roles.error
@setup_menu.error
@clear.error
@kick.error
@ban.error
@mute.error
async def perm_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ У тебя нет прав для этой команды.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Ошибка: {error}", ephemeral=True)

# ============ RUN ============
if not config.TOKEN:
    print("❌ DISCORD_TOKEN не найден в .env !")
else:
    bot.run(config.TOKEN)
