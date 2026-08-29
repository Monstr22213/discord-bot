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

# ============ ЭКОНОМИКА СПЕРМИКИ ============
ECONOMY_FILE = "economy.json"
QUEST_FILE = "quest_dima.json"

def load_economy():
    if not os.path.exists(ECONOMY_FILE):
        return {}
    try:
        with open(ECONOMY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_economy(data):
    try:
        with open(ECONOMY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass

def get_balance(user_id: int):
    data = load_economy()
    return data.get(str(user_id), {}).get("balance", 0)

def add_spermi(user_id: int, amount: int):
    data = load_economy()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {"balance": 0, "daily": 0}
    data[uid]["balance"] = data[uid].get("balance", 0) + amount
    save_economy(data)
    return data[uid]["balance"]

# Магазин
SHOP_ITEMS = {
    "бронь_от_дрона": {"price": 500, "desc": "Защита от дроноеба на 1 день", "role": None},
    "любимая_димы": {"price": 1000, "desc": "Роль Любимая Димы 💖 (легендарка)", "role": "Любимая Димы"},
    "глава_дроноеба": {"price": 777, "desc": "Роль Глава Дроноеба 🚁", "role": "Глава Дроноеба"},
    "vip_спермик": {"price": 300, "desc": "VIP роль + цвет", "role": "VIP Спермик"},
}

# Квест Димы - храним спрятанные сообщения
def load_quest():
    if not os.path.exists(QUEST_FILE):
        return {}
    try:
        with open(QUEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_quest(data):
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


# ============ EVENTS ============

@bot.event
async def on_ready():
    print(f"Бот {bot.user} запущен! На {len(bot.guilds)} серверах")
    bot.add_view(VerifyView())
    try:
        # Чистим дубли (было 2x /основа из-за copy_global_to) - оставляем только глобальные
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"Очищены дубли гильдии {config.GUILD_ID}")
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

@bot.tree.command(name="перевести", description="Перевести спермики другу")
async def transfer(interaction: discord.Interaction, пользователь: discord.Member, количество: app_commands.Range[int, 1, 10000]):
    if пользователь.id == interaction.user.id:
        await interaction.response.send_message("❌ Себе нельзя", ephemeral=True)
        return
    if get_balance(interaction.user.id) < количество:
        await interaction.response.send_message(f"❌ Недостаточно спермиков. Баланс: {get_balance(interaction.user.id)}", ephemeral=True)
        return
    add_spermi(interaction.user.id, -количество)
    add_spermi(пользователь.id, количество)
    await interaction.response.send_message(f"💸 {interaction.user.mention} перевел {количество} спермиков → {пользователь.mention}")

@bot.tree.command(name="магазин", description="Магазин за спермики")
async def shop(interaction: discord.Interaction):
    embed = discord.Embed(title="🛒 Магазин спермиков", color=discord.Color.gold())
    for key, item in SHOP_ITEMS.items():
        embed.add_field(name=f"{key} — {item['price']} спермиков", value=item['desc'], inline=False)
    embed.set_footer(text="Купить: /купить <название>")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="купить", description="Купить предмет из магазина")
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

# ============ КВЕСТ ЛЮБИМАЯ ДИМЫ ============
@bot.tree.command(name="квест-димы", description="Запустить квест Найди любимую Димы (только админ)")
@app_commands.checks.has_permissions(administrator=True)
async def quest_dima(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    # Выбираем 3 рандомных текстовых канала кроме бусификации
    channels = [c for c in guild.text_channels if "бусификация" not in c.name.lower()][:15]
    if len(channels) < 3:
        await interaction.followup.send("❌ Нужно минимум 3 канала", ephemeral=True)
        return
    picked = random.sample(channels, 3)
    quest_data = load_quest()
    quest_data[str(guild.id)] = {"channels": [c.id for c in picked], "found": {}}
    save_quest(quest_data)
    hidden_emoji = "💖"
    for ch in picked:
        try:
            await ch.send(f"||{hidden_emoji} любимая Димы спряталась тут! Напиши /нашел чтобы проверить||")
        except:
            pass
    await interaction.followup.send(f"✅ Квест запущен! Любимая Димы спрятана в 3 каналах: {', '.join([c.mention for c in picked])} (скрыто, ищи сам) + 500 спермиков за нахождение всех 3!", ephemeral=True)

@bot.tree.command(name="нашел", description="Проверить квест любимая Димы")
async def found_dima(interaction: discord.Interaction):
    guild = interaction.guild
    data = load_quest()
    q = data.get(str(guild.id))
    if not q:
        await interaction.response.send_message("❌ Квест не запущен. Админ: /квест-димы", ephemeral=True)
        return
    # Проверяем в каком канале вызвал
    ch_id = interaction.channel.id
    if ch_id not in q["channels"]:
        await interaction.response.send_message("❌ Тут нет любимой Димы, ищи дальше!", ephemeral=True)
        return
    uid = str(interaction.user.id)
    if uid not in q["found"]:
        q["found"][uid] = []
    if ch_id in q["found"][uid]:
        await interaction.response.send_message("Ты уже находил тут!", ephemeral=True)
        return
    q["found"][uid].append(ch_id)
    save_quest(data)
    left = 3 - len(q["found"][uid])
    if left > 0:
        await interaction.response.send_message(f"💖 Нашел! Осталось {left} шт. Ищи дальше!")
    else:
        add_spermi(interaction.user.id, 500)
        # Роль Любимая Димы
        role = discord.utils.get(guild.roles, name="Любимая Димы")
        if not role:
            role = await guild.create_role(name="Любимая Димы", colour=discord.Colour.pink())
        try:
            await interaction.user.add_roles(role)
        except:
            pass
        await interaction.response.send_message(f"🎉 КРАСАВА! Ты нашел все 3 любимые Димы! +500 спермиков и роль {role.mention}!")

# ============ РУЛЕТКА ГЛАВЫ ДРОНОЕБА ============
@bot.tree.command(name="рулетка-дроноеба", description="Рулетка главы дроноеба — испытай удачу")
async def roulete_drone(interaction: discord.Interaction, ставка: app_commands.Range[int, 1, 1000]):
    if get_balance(interaction.user.id) < ставка:
        await interaction.response.send_message(f"❌ Нужно {ставка} спермиков, у тебя {get_balance(interaction.user.id)}", ephemeral=True)
        return
    roll = random.randint(1, 100)
    if roll <= 45:
        # проиграл
        add_spermi(interaction.user.id, -ставка)
        await interaction.response.send_message(f"💥 Глава дроноеба ебанул! Ты проиграл {ставка} спермиков. Ролл {roll}/100")
    elif roll <= 90:
        win = int(ставка * 1.5)
        add_spermi(interaction.user.id, win)
        await interaction.response.send_message(f"🚁 Глава дроноеба промахнулся! Выиграл +{win} спермиков! Ролл {roll}/100. Баланс: {get_balance(interaction.user.id)}")
    else:
        win = ставка * 3
        add_spermi(interaction.user.id, win)
        await interaction.response.send_message(f"🔥 ДЖЕКПОТ! Глава дроноеба взорвался! +{win} спермиков! Ролл {roll}/100. Баланс: {get_balance(interaction.user.id)}")

# Обработка ошибок прав
@setup_verify.error
@setup_base.error
@setup_roles.error
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
