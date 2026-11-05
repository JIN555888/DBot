"""디스코드 스케줄 타이머 봇.

채널에 '보드' 메시지 하나를 올려 두고, 그 메시지만 주기적으로 수정해
타이머마다 진행률 바와 퍼센트를 갱신한다. 새 메시지를 계속 쌓지 않는다.
"""
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

BASE = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """.env 파일을 읽어 환경변수로 넣는다(이미 있는 값은 유지)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(BASE / ".env")

TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = os.environ.get("GUILD_ID", "").strip()          # 지정하면 그 서버에 명령을 즉시 등록
MAX_TIMERS = 20  # 2열 x 10줄 고정 (환경변수로 바꾸지 않음)
UPDATE_SEC = max(2, int(os.environ.get("UPDATE_SEC", "5")))  # 보드 수정 주기(초)
COLUMNS = 2
NOTIFY_DELETE_SEC = int(os.environ.get("NOTIFY_DELETE_SEC", "60"))  # 0 이면 알림 메시지를 지우지 않음
TZ = timezone(timedelta(hours=float(os.environ.get("TZ_HOURS", "9"))))
STATE_FILE = BASE / "state.json"
# git archive(deploy.sh)가 아래 $Format$ 자리를 커밋 해시로 바꾼다(.gitattributes export-subst).
# 보드 꼬리말에 보여 패치 반영 여부를 확인한다. 따로 파일을 읽지 않으므로 권한 문제로 죽지 않는다.
_VER = "$Format:%h$"
VERSION = "dev" if _VER.startswith("$") else _VER
MIN_PERIOD = 10

# ---------------------------------------------------------------- 시간 파싱/표시

_UNITS = {"일": 86400, "d": 86400, "시간": 3600, "시": 3600, "h": 3600,
          "분": 60, "m": 60, "초": 1, "s": 1}
_DUR_RE = re.compile(r"(\d+)(일|시간|시|분|초|d|h|m|s)")


def parse_duration(text: str) -> int:
    """'30m', '1h30m', '90s', '2시간', '45'(분) → 초."""
    s = text.strip().lower().replace(" ", "")
    if s.isdigit():
        return int(s) * 60
    total, pos = 0, 0
    for m in _DUR_RE.finditer(s):
        if m.start() != pos:
            break
        total += int(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if pos != len(s) or total <= 0:
        raise ValueError(f"주기 형식을 모르겠어요: `{text}` (예: 30m, 1h, 1h30m, 90s, 2시간)")
    return total


def fmt_duration(sec: int) -> str:
    parts = []
    for unit, size in (("일", 86400), ("시간", 3600), ("분", 60), ("초", 1)):
        if sec >= size:
            n, sec = divmod(sec, size)
            parts.append(f"{n}{unit}")
    return " ".join(parts) or "0초"


def fmt_remain(sec: float) -> str:
    sec = max(0, int(math.ceil(sec)))
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    if d:
        return f"{d}일 {h:02}:{m:02}:{s:02}"
    if h:
        return f"{h}:{m:02}:{s:02}"
    return f"{m:02}:{s:02}"


def parse_anchor(text: str | None, now: float) -> tuple[float, tuple[int, int, int] | None]:
    """기준 시각 → (anchor epoch, (시,분,초) 또는 None).

    None/빈값: 지금부터 시작. '정각': 00:00 기준. ':MM' / 'MM분': 매시 MM분 기준.
    'HH:MM' / 'HH:MM:SS': 그 시각 기준.
    """
    if not text or not text.strip():
        return now, None
    s = text.strip().replace(" ", "")
    hms = None
    if s == "정각":
        hms = (0, 0, 0)
    elif m := re.fullmatch(r":?(\d{1,2})분?", s):
        if s.startswith(":") or s.endswith("분"):
            hms = (0, int(m.group(1)), 0)
    if hms is None and (m := re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)):
        hms = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    if hms is None or hms[0] > 23 or hms[1] > 59 or hms[2] > 59:
        raise ValueError(f"기준 시각 형식을 모르겠어요: `{text}` (예: 정각, :30, 09:00)")
    day = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return (day + timedelta(hours=hms[0], minutes=hms[1], seconds=hms[2])).timestamp(), hms


def make_label(period: int, hms: tuple[int, int, int] | None, anchor: float) -> str:
    """반 칸에 들어가게 짧은 일정 설명."""
    if hms is None:
        return f"{fmt_duration(period)}마다"
    h, m, s = hms
    if period == 86400:
        return f"매일 {h:02}:{m:02}" + (f":{s:02}" if s else "")
    if period == 3600:
        return "매시 정각" if (m, s) == (0, 0) else f"매시 {m}분" + (f" {s}초" if s else "")
    if 86400 % period == 0 and (h, m, s) == (0, 0, 0):
        return f"{fmt_duration(period)}마다 (정각)"
    return f"{fmt_duration(period)}마다 ({h:02}:{m:02})"


def label_of(t: dict) -> str:
    if "hms" in t:  # 새 형식. 예전에 만든 타이머는 저장된 label 을 그대로 쓴다
        return make_label(t["period"], tuple(t["hms"]) if t["hms"] else None, t["anchor"])
    if "시작)" in t.get("label", ""):  # 예전 형식 '1분마다 (11:45:55 시작)' → 시작 시각은 빼고 보여 준다
        return make_label(t["period"], None, t["anchor"])
    return t["label"]


def progress(t: dict, now: float) -> tuple[float, float, int]:
    """(진행률 0~1, 남은 초, 지금까지 완료한 주기 번호)."""
    p = t["period"]
    d = now - t["anchor"]
    elapsed = d % p
    return elapsed / p, p - elapsed, math.floor(d / p)


# ---------------------------------------------------------------- 상태 저장


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"[warn] state.json 읽기 실패, 새로 시작: {e}")
    return {"guilds": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- 보드 그리기


BLANK = "\u200b"
EMOJI_CELLS = 10  # 이모지 한 칸 ≈ 20px, 반 칸(약 230px)에 10칸이 들어간다
NEAR, END = 0.75, 0.9  # 이 진행률부터 주황, 빨강


def level_color(frac: float) -> int:
    """줄(임베드) 왼쪽 띠 색: 파랑 → 주황 → 빨강."""
    return 0xED4245 if frac >= END else 0xF0B232 if frac >= NEAR else 0x5865F2


def emoji_bar(frac: float) -> str:
    """진행률에 따라 색이 바뀌는 이모지 바: 🟦 → 🟧(75%) → 🟥(90%)."""
    filled = min(EMOJI_CELLS, int(frac * EMOJI_CELLS))
    fill = "🟥" if frac >= END else "🟧" if frac >= NEAR else "🟦"
    return fill * filled + "⬛" * (EMOJI_CELLS - filled)


def card(t: dict, now: float) -> tuple[str, str, float]:
    """타이머 하나 → (필드 제목, 필드 내용, 진행률). 줄 사이에 빈 줄을 넣어 띄운다."""
    frac, remain, _ = progress(t, now)
    nxt = int(now + remain)
    bell = "  🔔" if t.get("notify") else ""
    title = f"{t['name']}{bell}"
    gap = f"\n{BLANK}\n"  # 필드 안에서는 ### 머리글이 안 먹어서 빈 줄로 간격을 만든다
    body = (
        f"{BLANK}\n"  # 제목과 바 사이
        f"{emoji_bar(frac)}{gap}"
        f"⏳ **{fmt_remain(remain)}** 남음 · **{int(frac * 100)}%**{gap}"  # 내림: 끝나기 전에 100% 로 안 보이게
        f"🔁 {label_of(t)} · <t:{nxt}:t>"
    )
    return title, body, frac


ROWS_PER_EMBED = 8  # 필드 25개 제한: 8줄 = 카드 16 + 줄바꿈 7 = 23개


def render(g: dict, now: float) -> list[discord.Embed]:
    """2열 격자 보드 하나. 16개(8줄)를 넘을 때만 아래에 임베드를 하나 더 붙인다."""
    timers = g["timers"]
    stamp = datetime.fromtimestamp(now, TZ).strftime("%H:%M:%S")
    footer = f"{len(timers)}/{MAX_TIMERS}개 · {UPDATE_SEC}초마다 갱신 · {stamp} · v{VERSION}"
    if not timers:
        e = discord.Embed(title="⏱ 스케줄 타이머", color=0x5865F2,
                          description="타이머가 없어요. 아래 **➕ 타이머 추가** 를 누르세요.")
        e.set_footer(text=footer)
        return [e]

    cards = [card(t, now) for t in timers]
    soonest = min(range(len(timers)), key=lambda i: progress(timers[i], now)[1])
    s_remain = progress(timers[soonest], now)[1]
    color = level_color(max(c[2] for c in cards))

    per_embed = COLUMNS * ROWS_PER_EMBED
    embeds = []
    for start in range(0, len(cards), per_embed):
        chunk = cards[start:start + per_embed]
        e = discord.Embed(color=color)
        if start == 0:
            e.title = "⏱ 스케줄 타이머"
            name = discord.utils.escape_markdown(timers[soonest]["name"])
            e.description = f"⏭ 다음 차례 **{name}** · <t:{int(now + s_remain)}:R>"
        for i in range(0, len(chunk), COLUMNS):
            row = chunk[i:i + COLUMNS]
            for title, body, _ in row:
                e.add_field(name=title, value=body, inline=True)
            for _ in range(COLUMNS - len(row)):  # 홀수 개면 빈 칸으로 2열 유지
                e.add_field(name=BLANK, value=BLANK, inline=True)
            if i + COLUMNS < len(chunk):  # 줄바꿈용 빈 필드 → 한 줄에 2개씩, 줄 사이 간격도 된다
                e.add_field(name=BLANK, value=BLANK, inline=False)
        embeds.append(e)
    embeds[-1].set_footer(text=footer)
    return embeds


# ---------------------------------------------------------------- 타이머 만들기/고치기 공통


def find(g: dict, name: str) -> dict | None:
    return next((t for t in g["timers"] if t["name"] == name), None)


def every_text(period: int) -> str:
    """초 → 입력창에 미리 채울 주기 문자열(1h30m)."""
    out = ""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        n, period = divmod(period, size)
        if n:
            out += f"{n}{unit}"
    return out or "0s"


def at_text(t: dict) -> str:
    """타이머 → 입력창에 미리 채울 기준 시각 문자열."""
    hms = t.get("hms")
    if not hms:
        return ""
    h, m, s = hms
    if (h, m, s) == (0, 0, 0):
        return "정각"
    if t["period"] <= 3600 and h == 0 and s == 0:
        return f":{m:02}"
    return f"{h:02}:{m:02}" + (f":{s:02}" if s else "")


def parse_notify(text: str | None) -> bool:
    return (text or "").strip().lower() in ("켬", "켜기", "on", "y", "yes", "1", "true", "예")


def upsert_timer(gid: str, channel_id: int, name: str, every: str, at: str | None,
                 notify: bool, original: str | None = None) -> dict:
    """타이머를 추가(original=None)하거나 고친다. 잘못된 입력은 ValueError."""
    g = bot.guild(int(gid))
    name = name.strip()
    if not name:
        raise ValueError("이름을 넣어 주세요.")
    old = find(g, original) if original else None
    if original and not old:
        raise ValueError(f"`{original}` 가 이미 없어요.")
    if name != original and find(g, name):
        raise ValueError(f"`{name}` 이미 있어요.")
    if not old and len(g["timers"]) >= MAX_TIMERS:
        raise ValueError(f"최대 {MAX_TIMERS}개까지예요. 하나 지우고 추가하세요.")
    now = time.time()
    period = parse_duration(every)
    if period < MIN_PERIOD:
        raise ValueError(f"주기는 {MIN_PERIOD}초 이상이어야 해요.")
    keep_running = bool(old) and not (at or "").strip() and not old.get("hms") and old["period"] == period
    if keep_running:  # 주기·기준이 그대로면 진행 중인 바를 이어 간다
        anchor, hms = old["anchor"], None
    else:
        anchor, hms = parse_anchor(at, now)
    t = {"name": name, "period": period, "anchor": anchor, "label": make_label(period, hms, anchor),
         "hms": list(hms) if hms else None, "notify": notify,
         "channel_id": old.get("channel_id", channel_id) if old else channel_id}
    if old:
        g["timers"][g["timers"].index(old)] = t
        bot.last_cycle.pop((gid, original), None)
    else:
        g["timers"].append(t)
    bot.last_cycle[(gid, name)] = progress(t, now)[2]
    bot.dirty.add(gid)
    save_state(bot.state)
    return t


def delete_timer(gid: str, name: str) -> bool:
    g = bot.guild(int(gid))
    t = find(g, name)
    if not t:
        return False
    g["timers"].remove(t)
    bot.last_cycle.pop((gid, name), None)
    bot.dirty.add(gid)
    save_state(bot.state)
    return True


# ---------------------------------------------------------------- 보드 아래 버튼·메뉴·입력창


class TimerModal(discord.ui.Modal):
    def __init__(self, gid: str, original: str | None = None) -> None:
        super().__init__(title="타이머 수정" if original else "타이머 추가", timeout=600)
        self.gid, self.original = gid, original
        t = find(bot.guild(int(gid)), original) if original else None
        self.name_in = discord.ui.TextInput(label="이름", max_length=32,
                                            default=t["name"] if t else None, placeholder="예: 회의")
        self.every_in = discord.ui.TextInput(label="주기", max_length=20,
                                             default=every_text(t["period"]) if t else None,
                                             placeholder="30m, 1h, 1h30m, 90s, 1d, 2시간")
        self.at_in = discord.ui.TextInput(label="기준 시각 (비우면 지금부터)", required=False, max_length=10,
                                          default=(at_text(t) or None) if t else None,
                                          placeholder="정각 / :30 / 09:00")
        self.notify_in = discord.ui.TextInput(label="알림 (켬 / 끔)", required=False, max_length=4,
                                              default=("켬" if t.get("notify") else "끔") if t else "끔")
        for item in (self.name_in, self.every_in, self.at_in, self.notify_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            t = upsert_timer(self.gid, interaction.channel_id, self.name_in.value, self.every_in.value,
                             self.at_in.value, parse_notify(self.notify_in.value), self.original)
        except ValueError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        verb = "수정" if self.original else "추가"
        await interaction.response.send_message(f"{verb}: **{t['name']}** · {label_of(t)}",
                                                ephemeral=True, delete_after=5)
        await bot.refresh_board(self.gid)


def _placeholder_opts() -> list[discord.SelectOption]:
    return [discord.SelectOption(label="타이머 없음", value="_none")]


class BoardView(discord.ui.View):
    """보드 메시지에 붙는 버튼·메뉴. custom_id 고정 → 봇을 다시 켜도 동작한다."""

    def __init__(self, timers: list[dict] | None = None) -> None:
        super().__init__(timeout=None)
        opts = [discord.SelectOption(label=t["name"][:100], value=t["name"][:100],
                                     description=label_of(t)[:100]) for t in (timers or [])[:25]]
        for sel in (self.edit_sel, self.del_sel):
            sel.options = opts or _placeholder_opts()
            sel.disabled = not opts

    @discord.ui.button(label="타이머 추가", emoji="➕", style=discord.ButtonStyle.success,
                       custom_id="dbot:add", row=0)
    async def add_btn(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if len(bot.guild(interaction.guild_id)["timers"]) >= MAX_TIMERS:
            return await interaction.response.send_message(
                f"최대 {MAX_TIMERS}개까지예요. 하나 지우고 추가하세요.", ephemeral=True)
        await interaction.response.send_modal(TimerModal(str(interaction.guild_id)))

    @discord.ui.select(custom_id="dbot:edit", placeholder="✏️ 수정할 타이머 (이름·시간 바꾸기)",
                       options=_placeholder_opts(), row=1)
    async def edit_sel(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        gid = str(interaction.guild_id)
        bot.dirty.add(gid)  # 다음 갱신 때 메뉴에 남은 선택 표시를 지운다
        name = select.values[0]
        if not find(bot.guild(interaction.guild_id), name):
            return await interaction.response.send_message(f"`{name}` 가 이미 없어요.", ephemeral=True)
        await interaction.response.send_modal(TimerModal(gid, original=name))

    @discord.ui.select(custom_id="dbot:del", placeholder="🗑️ 삭제할 타이머",
                       options=_placeholder_opts(), row=2)
    async def del_sel(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        gid = str(interaction.guild_id)
        name = select.values[0]
        bot.dirty.add(gid)
        ok = delete_timer(gid, name)
        await interaction.response.send_message(f"삭제: **{name}**" if ok else f"`{name}` 가 이미 없어요.",
                                                ephemeral=True, delete_after=5)
        await bot.refresh_board(gid)


# ---------------------------------------------------------------- 봇


class TimerBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.state = load_state()
        self.last_cycle: dict[tuple[str, str], int] = {}
        self.dirty: set[str] = set(self.state["guilds"])  # 버튼·메뉴를 다시 붙여야 하는 서버
        self.tick_count = 0

    def guild(self, gid: int) -> dict:
        return self.state["guilds"].setdefault(str(gid), {"board": None, "timers": []})

    async def setup_hook(self) -> None:
        self.tree.add_command(timer_group)
        self.add_view(BoardView())  # 재시작 전에 올린 보드의 버튼·메뉴도 받는다
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()
        now = time.time()
        for gid, g in self.state["guilds"].items():
            for t in g["timers"]:
                self.last_cycle[(gid, t["name"])] = progress(t, now)[2]
        self.ticker.start()

    async def on_ready(self) -> None:
        print(f"[ok] {self.user} 로그인, 서버 {len(self.guilds)}개")

    async def refresh_board(self, gid: str) -> None:
        g = self.state["guilds"].get(gid)
        if not g or not g.get("board"):
            return
        b = g["board"]
        channel = self.get_channel(b["channel_id"])
        # 메뉴는 타이머 목록이 바뀔 때만 다시 붙인다(매번 붙이면 열어 둔 메뉴가 닫힌다)
        with_view = gid in self.dirty
        kwargs = {"view": BoardView(g["timers"])} if with_view else {}
        try:
            if channel is None:
                channel = await self.fetch_channel(b["channel_id"])
            await channel.get_partial_message(b["message_id"]).edit(embeds=render(g, time.time()), **kwargs)
            if with_view:
                self.dirty.discard(gid)
        except discord.NotFound:
            print(f"[warn] 서버 {gid} 보드 메시지가 사라짐 → 보드 해제")
            g["board"] = None
            save_state(self.state)
        except discord.Forbidden:
            print(f"[warn] 서버 {gid} 보드 채널 권한 없음")
        except discord.HTTPException as e:
            print(f"[warn] 서버 {gid} 보드 갱신 실패: {e}")

    async def notify(self, gid: str, t: dict) -> None:
        g = self.state["guilds"][gid]
        cid = (g.get("board") or {}).get("channel_id") or t.get("channel_id")
        channel = self.get_channel(cid) if cid else None
        if channel is None:
            return
        try:
            await channel.send(
                f"⏰ **{discord.utils.escape_markdown(t['name'])}** — {label_of(t)}",
                delete_after=NOTIFY_DELETE_SEC or None,
            )
        except discord.HTTPException as e:
            print(f"[warn] 알림 실패: {e}")

    @tasks.loop(seconds=1)
    async def ticker(self) -> None:
        now = time.time()
        # 주기 완료 감지(1초 단위)
        for gid, g in self.state["guilds"].items():
            for t in g["timers"]:
                key = (gid, t["name"])
                cyc = progress(t, now)[2]
                prev = self.last_cycle.get(key)
                self.last_cycle[key] = cyc
                if prev is not None and cyc > prev and t.get("notify"):
                    await self.notify(gid, t)
        # 보드 갱신(UPDATE_SEC 단위)
        self.tick_count += 1
        if self.tick_count % UPDATE_SEC == 0:
            for gid in list(self.state["guilds"]):
                await self.refresh_board(gid)

    @ticker.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()


bot = TimerBot()
timer_group = app_commands.Group(name="timer", description="스케줄 타이머", guild_only=True)


async def name_autocomplete(interaction: discord.Interaction, current: str):
    g = bot.guild(interaction.guild_id)
    return [app_commands.Choice(name=t["name"], value=t["name"])
            for t in g["timers"] if current.lower() in t["name"].lower()][:25]


async def reply_and_refresh(interaction: discord.Interaction, text: str) -> None:
    await interaction.response.send_message(text, ephemeral=True)
    await bot.refresh_board(str(interaction.guild_id))


@timer_group.command(name="add", description="타이머 추가")
@app_commands.describe(
    name="타이머 이름",
    every="주기 (예: 30m, 1h, 1h30m, 90s, 1d, 2시간)",
    at="기준 시각 (비우면 지금부터 / 정각 / :30 / 09:00)",
    notify="한 바퀴 돌 때마다 채널에 알림 (기본 끔)",
)
async def add_cmd(interaction: discord.Interaction, name: app_commands.Range[str, 1, 32],
                  every: str, at: str | None = None, notify: bool = False):
    try:
        t = upsert_timer(str(interaction.guild_id), interaction.channel_id, name, every, at, notify)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    has_board = bot.guild(interaction.guild_id).get("board")
    hint = "" if has_board else "\n보드가 없어요. `/timer board` 로 이 채널에 띄우세요."
    await reply_and_refresh(interaction, f"추가: **{t['name']}** · {label_of(t)}{hint}")


@timer_group.command(name="remove", description="타이머 삭제")
@app_commands.autocomplete(name=name_autocomplete)
async def remove_cmd(interaction: discord.Interaction, name: str):
    ok = delete_timer(str(interaction.guild_id), name)
    await reply_and_refresh(interaction, f"삭제: **{name}**" if ok else f"`{name}` 없어요.")


@timer_group.command(name="reset", description="타이머를 지금부터 다시 시작")
@app_commands.autocomplete(name=name_autocomplete)
async def reset_cmd(interaction: discord.Interaction, name: str):
    gid = str(interaction.guild_id)
    t = find(bot.guild(interaction.guild_id), name)
    if not t:
        return await interaction.response.send_message(f"`{name}` 없어요.", ephemeral=True)
    now = time.time()
    t["anchor"] = now
    t["label"] = make_label(t["period"], None, now)
    t["hms"] = None
    bot.last_cycle[(gid, name)] = 0
    bot.dirty.add(gid)
    save_state(bot.state)
    await reply_and_refresh(interaction, f"리셋: **{name}** · {label_of(t)}")


@timer_group.command(name="refresh", description="보드를 지금 다시 그림 (패치 후 모양·버튼 반영)")
async def refresh_cmd(interaction: discord.Interaction):
    gid = str(interaction.guild_id)
    if not bot.guild(interaction.guild_id).get("board"):
        return await interaction.response.send_message(
            "보드가 없어요. `/timer board` 로 이 채널에 띄우세요.", ephemeral=True)
    bot.dirty.add(gid)  # 버튼·메뉴까지 새 코드로 다시 붙인다
    await interaction.response.send_message(f"보드를 다시 그렸어요 (v{VERSION}).", ephemeral=True, delete_after=5)
    await bot.refresh_board(gid)


@timer_group.command(name="list", description="타이머 목록 (나만 보기)")
async def list_cmd(interaction: discord.Interaction):
    g = bot.guild(interaction.guild_id)
    await interaction.response.send_message(embeds=render(g, time.time()), ephemeral=True)


@timer_group.command(name="board", description="이 채널에 보드(계속 갱신되는 메시지)를 띄움")
async def board_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    g = bot.guild(interaction.guild_id)
    old = g.get("board")
    try:
        msg = await interaction.channel.send(embeds=render(g, time.time()), view=BoardView(g["timers"]))
    except discord.HTTPException as e:
        return await interaction.followup.send(f"이 채널에 메시지를 못 올려요(권한 확인): {e}", ephemeral=True)
    g["board"] = {"channel_id": msg.channel.id, "message_id": msg.id}
    save_state(bot.state)
    if old:  # 예전 보드는 지워서 화면에 하나만 남긴다
        try:
            ch = bot.get_channel(old["channel_id"]) or await bot.fetch_channel(old["channel_id"])
            await ch.get_partial_message(old["message_id"]).delete()
        except discord.HTTPException:
            pass
    await interaction.followup.send("보드를 띄웠어요. 이 메시지만 계속 갱신돼요.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN 이 없어요. .env.example 을 .env 로 복사해 토큰을 넣으세요.")
    bot.run(TOKEN)
