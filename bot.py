import discord
from discord import app_commands
from discord.ext import commands
import config
from datetime import timedelta

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ============ VIEWS ============

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Верифицироваться", style=discord.ButtonStyle.green, custom_id="verify_btn")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = interaction.guild.get_role(config.VERIFY_ROLE_ID) if config.VERIFY_ROLE_ID else None
        if not role:
            await interaction.response.send_message("❌ Роль для верификации не настроена. Обратись к админу.", ephemeral=True)
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
    # Для RolesView нужно guild, поэтому добавляем после готовности если нужно persistent
    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано {len(synced)} команд")
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
        title="🔐 Верификация",
        description="Нажми на кнопку ниже, чтобы получить доступ к серверу!\n\n> После нажатия тебе выдастся основная роль и откроются каналы.",
        color=discord.Color.blurple()
    )
    embed.set_footer(text=interaction.guild.name)
    await ch.send(embed=embed, view=VerifyView())
    await interaction.response.send_message(f"✅ Панель верификации создана в {ch.mention}", ephemeral=True)

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

# Обработка ошибок прав
@setup_verify.error
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
