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
MAX_TIMERS = max(1, min(int(os.environ.get("MAX_TIMERS", "10")), 25))
UPDATE_SEC = max(2, int(os.environ.get("UPDATE_SEC", "5")))  # 보드 수정 주기(초)
BAR_LEN = max(5, min(int(os.environ.get("BAR_LEN", "20")), 40))
NOTIFY_DELETE_SEC = int(os.environ.get("NOTIFY_DELETE_SEC", "60"))  # 0 이면 알림 메시지를 지우지 않음
TZ = timezone(timedelta(hours=float(os.environ.get("TZ_HOURS", "9"))))
STATE_FILE = BASE / "state.json"
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
    if hms is None:
        start = datetime.fromtimestamp(anchor, TZ).strftime("%H:%M:%S")
        return f"{fmt_duration(period)}마다 ({start} 시작)"
    h, m, s = hms
    if period == 86400:
        return f"매일 {h:02}:{m:02}" + (f":{s:02}" if s else "")
    if period == 3600:
        return "매시 정각" if (m, s) == (0, 0) else f"매시 {m}분" + (f" {s}초" if s else "")
    if 86400 % period == 0 and (h, m, s) == (0, 0, 0):
        return f"{fmt_duration(period)}마다 (정각 기준)"
    return f"{fmt_duration(period)}마다 ({h:02}:{m:02} 기준)"


def progress(t: dict, now: float) -> tuple[float, float, int]:
    """(진행률 0~1, 남은 초, 지금까지 완료한 주기 번호)."""
    p = t["period"]
    d = now - t["anchor"]
    elapsed = d % p
    return elapsed / p, p - elapsed, math.floor(d / p)


def bar(frac: float) -> str:
    filled = min(BAR_LEN, int(frac * BAR_LEN))
    return "█" * filled + "░" * (BAR_LEN - filled)


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


# ---------------------------------------------------------------- 봇


def render(g: dict, now: float) -> discord.Embed:
    blocks = []
    for t in g["timers"]:
        frac, remain, _ = progress(t, now)
        nxt = int(now + remain)
        icon = "🔥" if frac >= 0.9 else "⏳"
        bell = " 🔔" if t.get("notify") else ""
        name = discord.utils.escape_markdown(t["name"])
        blocks.append(
            f"{icon} **{name}** · {t['label']}{bell}\n"
            f"`{bar(frac)} {frac * 100:5.1f}%`  남은 **{fmt_remain(remain)}** · 다음 <t:{nxt}:T>"
        )
    embed = discord.Embed(
        title="⏱ 스케줄 타이머",
        description="\n\n".join(blocks) or "타이머가 없어요. `/timer add` 로 추가하세요.",
        color=0x5865F2,
    )
    stamp = datetime.fromtimestamp(now, TZ).strftime("%H:%M:%S")
    embed.set_footer(text=f"{len(g['timers'])}/{MAX_TIMERS}개 · {UPDATE_SEC}초마다 갱신 · 마지막 {stamp}")
    return embed


class TimerBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.state = load_state()
        self.last_cycle: dict[tuple[str, str], int] = {}
        self.tick_count = 0

    def guild(self, gid: int) -> dict:
        return self.state["guilds"].setdefault(str(gid), {"board": None, "timers": []})

    async def setup_hook(self) -> None:
        self.tree.add_command(timer_group)
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
        try:
            if channel is None:
                channel = await self.fetch_channel(b["channel_id"])
            await channel.get_partial_message(b["message_id"]).edit(embed=render(g, time.time()))
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
                f"⏰ **{discord.utils.escape_markdown(t['name'])}** — {t['label']}",
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


def find(g: dict, name: str) -> dict | None:
    return next((t for t in g["timers"] if t["name"] == name), None)


async def name_autocomplete(interaction: discord.Interaction, current: str):
    g = bot.guild(interaction.guild_id)
    return [app_commands.Choice(name=t["name"], value=t["name"])
            for t in g["timers"] if current.lower() in t["name"].lower()][:25]


@timer_group.command(name="add", description="타이머 추가")
@app_commands.describe(
    name="타이머 이름",
    every="주기 (예: 30m, 1h, 1h30m, 90s, 1d, 2시간)",
    at="기준 시각 (비우면 지금부터 / 정각 / :30 / 09:00)",
    notify="한 바퀴 돌 때마다 채널에 알림 (기본 끔)",
)
async def add_cmd(interaction: discord.Interaction, name: app_commands.Range[str, 1, 32],
                  every: str, at: str | None = None, notify: bool = False):
    g = bot.guild(interaction.guild_id)
    if find(g, name):
        return await interaction.response.send_message(f"`{name}` 이미 있어요.", ephemeral=True)
    if len(g["timers"]) >= MAX_TIMERS:
        return await interaction.response.send_message(
            f"최대 {MAX_TIMERS}개까지예요. `/timer remove` 로 지우고 추가하세요.", ephemeral=True)
    now = time.time()
    try:
        period = parse_duration(every)
        if period < MIN_PERIOD:
            raise ValueError(f"주기는 {MIN_PERIOD}초 이상이어야 해요.")
        anchor, hms = parse_anchor(at, now)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    t = {"name": name, "period": period, "anchor": anchor, "label": make_label(period, hms, anchor),
         "notify": notify, "channel_id": interaction.channel_id}
    g["timers"].append(t)
    bot.last_cycle[(str(interaction.guild_id), name)] = progress(t, now)[2]
    save_state(bot.state)
    hint = "" if g.get("board") else "\n보드가 없어요. `/timer board` 로 이 채널에 띄우세요."
    await interaction.response.send_message(f"추가: **{name}** · {t['label']}{hint}", ephemeral=True)
    await bot.refresh_board(str(interaction.guild_id))


@timer_group.command(name="remove", description="타이머 삭제")
@app_commands.autocomplete(name=name_autocomplete)
async def remove_cmd(interaction: discord.Interaction, name: str):
    g = bot.guild(interaction.guild_id)
    t = find(g, name)
    if not t:
        return await interaction.response.send_message(f"`{name}` 없어요.", ephemeral=True)
    g["timers"].remove(t)
    bot.last_cycle.pop((str(interaction.guild_id), name), None)
    save_state(bot.state)
    await interaction.response.send_message(f"삭제: **{name}**", ephemeral=True)
    await bot.refresh_board(str(interaction.guild_id))


@timer_group.command(name="reset", description="타이머를 지금부터 다시 시작")
@app_commands.autocomplete(name=name_autocomplete)
async def reset_cmd(interaction: discord.Interaction, name: str):
    g = bot.guild(interaction.guild_id)
    t = find(g, name)
    if not t:
        return await interaction.response.send_message(f"`{name}` 없어요.", ephemeral=True)
    now = time.time()
    t["anchor"] = now
    t["label"] = make_label(t["period"], None, now)
    bot.last_cycle[(str(interaction.guild_id), name)] = 0
    save_state(bot.state)
    await interaction.response.send_message(f"리셋: **{name}** · {t['label']}", ephemeral=True)
    await bot.refresh_board(str(interaction.guild_id))


@timer_group.command(name="list", description="타이머 목록 (나만 보기)")
async def list_cmd(interaction: discord.Interaction):
    g = bot.guild(interaction.guild_id)
    await interaction.response.send_message(embed=render(g, time.time()), ephemeral=True)


@timer_group.command(name="board", description="이 채널에 보드(계속 갱신되는 메시지)를 띄움")
async def board_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    g = bot.guild(interaction.guild_id)
    old = g.get("board")
    try:
        msg = await interaction.channel.send(embed=render(g, time.time()))
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
