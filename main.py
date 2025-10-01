import asyncio
import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import html
from fastapi import FastAPI, Request
from aiogram import types
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message, Chat
from dotenv import load_dotenv

# ========= Константы/настройки =========
load_dotenv()
TOKEN = os.getenv("TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BASE_URL = os.getenv("BASE_URL")                  # напр. https://parahod.onrender.com
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret")
# дата начала семестра: 1-я учебная неделя = неделя, начинающаяся в этот день
SEMESTER_START_ENV = os.getenv("SEMESTER_START")  # YYYY-MM-DD
DATA_DIR = Path("./data"); DATA_DIR.mkdir(exist_ok=True)
SCHEDULE_FILE = DATA_DIR / "schedules_semester.json"
USERS_FILE = DATA_DIR / "users.json"
SEM_FILE = DATA_DIR / "semester.json"

# фиксированные слоты
SLOTS = {
    1: "8:30-10:05",
    2: "10:20-11:55",
    3: "12:10-13:45",
    4: "14:45-16:20",
}

RU_WEEKDAYS = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]


# ===== Валидатор аудитории =====
ROOM_DIGITS_RE = re.compile(r"^\d{1,5}$")  # допускаем 1..5 цифр (подправь при желании)
ROOM_CANON = {"д/о": "д/о", "с/зал": "с/зал"}

def _normalize_room_token(tok: str) -> str:
    # приведение к нижнему регистру + нормализация слеша
    return tok.strip().lower().replace("\\", "/")

def _parse_room_from_tokens(tokens: List[str], start_idx: int):
    """
    Берём РОВНО один токен аудитории, начиная с start_idx.
    Разрешено: только цифры ИЛИ 'д/о' ИЛИ 'с/зал'.
    """
    if start_idx >= len(tokens):
        return None, "Не указана аудитория. Разрешены только цифры, 'д/о' или 'с/зал'."
    # аудитория должна быть одним «словом»
    if len(tokens[start_idx:]) != 1:
        return None, "Аудитория должна быть одним словом: цифры, 'д/о' или 'с/зал'."

    raw = _normalize_room_token(tokens[start_idx])
    if raw in ROOM_CANON:
        return ROOM_CANON[raw], None
    if ROOM_DIGITS_RE.fullmatch(raw):
        return raw, None
    return None, "Недопустимая аудитория. Разрешены только цифры, 'д/о' или 'с/зал'."



# ========= Модель =========
@dataclass
class Lesson:
    title: str
    room: str
    weeks_spec: str        # исходная строка, напр. "2-4,10"
    weeks: List[int]       # нормализованный список недель (1..N)
    parity: Optional[str]  # "odd" | "even" | None

def _load_json(path: Path, default: Any):
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError:
            return default
    return default

def _save_json(path: Path, data: Any):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_users() -> Dict[str, str]:
    return _load_json(USERS_FILE, {})

def save_users(d: Dict[str, str]):
    _save_json(USERS_FILE, d)

def load_semester_start() -> Optional[date]:
    # приоритет: файл -> .env
    d = _load_json(SEM_FILE, {})
    s = d.get("semester_start") or SEMESTER_START_ENV
    if not s: return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None

def save_semester_start(dstr: str):
    _save_json(SEM_FILE, {"semester_start": dstr})

def load_schedules() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Структура:
    schedules[group][day][slot] = Lesson-dict
    """
    return _load_json(SCHEDULE_FILE, {})

def save_schedules(d: Dict[str, Dict[str, Dict[str, Any]]]):
    _save_json(SCHEDULE_FILE, d)

# ========= Парсинг недель =========
range_re = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
num_re = re.compile(r"^\s*(\d+)\s*$")

def parse_weeks(spec: str, max_week: int = 30) -> List[int]:
    """
    Превращает "2-4,10" -> [2,3,4,10]
    Защита от странных значений и дубликатов, ограничение по max_week.
    """
    if not spec:
        # пусто = считаем что пара доступна на всех неделях 1..max_week
        return list(range(1, max_week + 1))
    weeks: Set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part: continue
        m = range_re.match(part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b: a, b = b, a
            for w in range(a, b + 1):
                if 1 <= w <= max_week: weeks.add(w)
            continue
        m = num_re.match(part)
        if m:
            w = int(m.group(1))
            if 1 <= w <= max_week: weeks.add(w)
    return sorted(weeks) if weeks else list(range(1, max_week + 1))

def detect_parity(token: str) -> Optional[str]:
    t = token.lower().replace(" ", "")
    if t in ("н/н", "н-н", "нн", "odd"): return "odd"
    if t in ("ч/н", "ч-н", "чн", "even"): return "even"
    return None

def week_number(today: date, start: date) -> int:
    """1-я неделя начинается в день start; каждые 7 дней новая неделя."""
    delta_days = (today - start).days
    return (delta_days // 7) + 1 if delta_days >= 0 else 0  # 0 => до начала семестра

def weekday_ru(dt: datetime) -> str:
    return RU_WEEKDAYS[dt.weekday()]

def lesson_matches_week(lesson: Lesson, wk: int) -> bool:
    if wk <= 0:  # до начала семестра — ничего не показываем
        return False
    if wk not in lesson.weeks:
        return False
    if lesson.parity == "odd" and wk % 2 == 0:
        return False
    if lesson.parity == "even" and wk % 2 == 1:
        return False
    return True

# ========= Инициализация бота =========
bot = Bot(TOKEN)
dp = Dispatcher()

# ========= Форматирование =========
def format_day(group: str, day: str, wk: int, schedules: Dict[str, Dict[str, Dict[str, Any]]]) -> str:
    items = []
    group_data = schedules.get(group, {})
    day_data = group_data.get(day, {})
    for slot in sorted(SLOTS.keys()):
        raw = day_data.get(str(slot))
        if not raw:
            continue
        lesson = Lesson(**raw)
        if lesson_matches_week(lesson, wk):
            title = sanitize_title(lesson.title)
            badge = room_badge(lesson.room)
            items.append(f"{slot}) <code>{SLOTS[slot]}</code> • {e(title)}  &lt;{badge}{e(lesson.room)}&gt;")
    head = f"📅 <b>{e(group)} — {e(day.capitalize())}</b> • неделя #{wk}"
    return head + ("\n" + "\n".join(items) if items else "\nПар нет.")

def format_week(group: str, start_dt: date, schedules: Dict[str, Dict[str, Dict[str, Any]]]) -> str:
    today = datetime.now().date()
    wk = week_number(today, start_dt)
    lines = [f"🗓️ <b>{e(group)} — расписание на неделю (текущая неделя #{wk})</b>"]
    for dname in RU_WEEKDAYS:
        lines.append(f"\n<b>{e(dname.capitalize())}</b>")
        day_data = schedules.get(group, {}).get(dname, {})
        any_shown = False
        for slot in sorted(SLOTS.keys()):
            raw = day_data.get(str(slot))
            if not raw:
                continue
            lesson = Lesson(**raw)
            if lesson_matches_week(lesson, wk):
                title = sanitize_title(lesson.title)
                badge = room_badge(lesson.room)
                lines.append(f"{slot}) <code>{SLOTS[slot]}</code> • {e(title)}  &lt;{badge}{e(lesson.room)}&gt;")
                any_shown = True
        if not any_shown:
            lines.append("— нет пар")
    return "\n".join(lines)



DAY_SET = set(RU_WEEKDAYS)

def sanitize_title(title: str) -> str:
    """
    Убирает в начале названия шум вида: '01 понедельник 2 ', 'среда 1 ', 'понедельник', '01' и т.п.
    Только если это действительно про день/номер.
    """
    t = title.strip()
    low = t.lower()

    # убираем лидирующие числа (0..2 цифры — часто '01'/'1')
    i = 0
    while i < len(low) and low[i].isdigit():
        i += 1
    if i > 0 and i <= 2:
        low = low[i:].lstrip()
        t = t[i:].lstrip()

    # если дальше идёт название дня — вырежем его и возможный номер слота
    for day in RU_WEEKDAYS:
        if low.startswith(day):
            # срезаем сам день
            pos = len(day)
            low = low[pos:].lstrip()
            t   = t[pos:].lstrip()
            # срежем одиночное число (типа '2') сразу после дня
            j = 0
            while j < len(low) and low[j].isdigit():
                j += 1
            if j == 1:  # слот — одна цифра 1..4
                low = low[j:].lstrip()
                t   = t[j:].lstrip()
            break

    # финальная зачистка двойных пробелов
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t

def room_badge(room: str) -> str:
    r = room.lower()
    if r == "д/о":
        return "🖥️"
    if r == "с/зал":
        return "🏟️"
    return "🏫"  # цифры

def e(s: str) -> str:
    return html.escape(s)



# ========= Команды пользователя =========
@dp.message(CommandStart())
async def start(m: Message):
    sem = load_semester_start()
    sem_info = f"\nТекущая дата начала семестра: {sem}" if sem else "\nДата начала семестра пока не задана (попроси админа /set_semester YYYY-MM-DD)."
    await m.answer(
        "Привет! Я покажу расписание с учётом номера учебной недели и чётности.\n\n"
        "Команды:\n"
        "• /parahod сегодня [группа]\n"
        "• /parahod завтра [группа]\n"
        "• /parahod неделя [группа]\n"
        "• /setgroup <группа> — запомнить группу\n"
        + sem_info
    )

def scope_id(m: Message) -> str:
    # одна настройка на чат/супергруппу/канал
    if m.chat.type in {"group", "supergroup", "channel"}:
        return str(m.chat.id)
    # в личке — персональная настройка
    return str(m.from_user.id)

async def is_chat_admin(message: Message) -> bool:
    # в личке — всегда ок
    if message.chat.type == "private":
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in {"administrator", "creator"}
    except Exception:
        return False

async def _setgroup_impl(m: Message, args: str):
    args = (args or "").strip()
    # если аргумента нет — подсказываем, куда сохраняем
    if not args:
        target = "для этого чата" if m.chat.type != "private" else "для тебя"
        await m.answer(f"Укажи учебную группу {target}: /setgroup ИВТ-21")
        return

    # в группах/каналах менять может только админ
    if m.chat.type in {"group", "supergroup", "channel"}:
        if not await is_chat_admin(m):
            await m.answer("Только админ этого чата/канала может менять учебную группу.")
            return

    users = load_users()
    users[scope_id(m)] = args
    save_users(users)
    where = "для этого чата" if m.chat.type in {"group","supergroup","channel"} else "для тебя"
    await m.answer(f"Ок, запомнил {where}: <b>{html.escape(args)}</b>", parse_mode="HTML")

@dp.message(Command("setgroup"))
async def setgroup_msg(m: Message, command: CommandObject):
    await _setgroup_impl(m, command.args)

@dp.channel_post(Command("setgroup"))
async def setgroup_channel(m: Message, command: CommandObject):
    await _setgroup_impl(m, command.args)


@dp.message(Command("parahod"))
async def parahod(m: Message, command: CommandObject):
    raw = (command.args or "").strip()
    parts = raw.split()
    if not parts:
        await m.answer("Формат: /parahod сегодня|завтра|неделя [учебная_группа]")
        return

    mode = parts[0].lower()
    # если в команде явно указали учебную группу — используем её
    if len(parts) > 1:
        group = " ".join(parts[1:])
    else:
        group = load_users().get(scope_id(m))  # берем сохранённую для этого чата/пользователя

    if not group:
        hint = ("Сначала укажи учебную группу для чата: /setgroup ИВТ-21"
                if m.chat.type in {"group","supergroup"}
                else "Укажи учебную группу: /setgroup ИВТ-21")
        await m.answer(hint)
        return

    sem = load_semester_start()
    if not sem:
        await m.answer("Не задана дата начала семестра. Попроси админа: /set_semester YYYY-MM-DD")
        return

    schedules = load_schedules()
    now = datetime.now()
    if mode in ("сегодня", "segodnya"):
        wk = week_number(now.date(), sem)
        day = weekday_ru(now)
        await m.answer(format_day(group, day, wk, schedules), parse_mode="HTML")
    elif mode in ("завтра", "zavtra"):
        tmr = now + timedelta(days=1)
        wk = week_number(tmr.date(), sem)
        day = weekday_ru(tmr)
        await m.answer(format_day(group, day, wk, schedules), parse_mode="HTML")
    elif mode in ("неделя", "неделю", "nedelya"):
        await m.answer(format_week(group, sem, schedules), parse_mode="HTML")
    else:
        await m.answer("Не понял режим. Используй: сегодня | завтра | неделя")

@dp.channel_post(Command("parahod"))
async def parahod_channel(m: Message, command: CommandObject):
    # переиспользуем существующую логику для Message
    await parahod(m, command)

# ========= Админ-команды =========
def is_admin(uid: int) -> bool:
    return ADMIN_ID and uid == ADMIN_ID

@dp.message(Command("set_semester"))
async def set_semester(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        await m.answer("Только для админа.")
        return
    d = (command.args or "").strip()
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except Exception:
        await m.answer("Формат: /set_semester YYYY-MM-DD")
        return
    save_semester_start(d)
    await m.answer(f"✅ Дата начала семестра установлена: {d}")

def parse_add_lesson_args(args: str):
    """
    Ожидаем: <группа> <день> <слот> <Название(недели)> [н/н|ч/н] <аудитория>
    Примеры:
      ИВТ-21 понедельник 2 Алгебра(2-13) н/н 101
      ИВТ-21 вторник 1 Физика(2-4,10) 202
      ИВТ-21 среда 3 Программирование( ) ч/н д/о
      ИВТ-21 четверг 4 Спорт(1-16) с/зал
    Ограничение аудитории: только цифры, 'д/о' или 'с/зал'.
    """
    parts = args.split()
    if len(parts) < 4:
        return None, "Минимум 4 части: <группа> <день> <слот> <Название(недели)> ..."

    group = parts[0]
    day = parts[1].lower()
    if day not in RU_WEEKDAYS:
        return None, f"День должен быть одним из: {', '.join(RU_WEEKDAYS)}"

    try:
        slot = int(parts[2])
        if slot not in SLOTS:
            return None, f"Слот должен быть 1..{len(SLOTS)}"
    except ValueError:
        return None, "Слот должен быть числом 1..4"

    # восстановим хвост, начиная с названия
    tail = args.split(parts[2], 1)[1].strip()
    if tail.startswith(str(slot)):
        tail = tail[len(str(slot)):].strip()

    # найдём первую пару скобок с неделями
    l = tail.find("(")
    r = tail.find(")")
    if l == -1 or r == -1 or r < l:
        return None, "Не нашёл скобки с неделями. Пример: Алгебра(2-13)"

    title = tail[:l].strip()
    weeks_raw = tail[l+1:r].strip()  # может быть пусто => все недели
    after = tail[r+1:].strip()

    parity: Optional[str] = None
    room: Optional[str] = None

    if after:
        tokens = after.split()
        # пробуем вытащить чётность первым токеном
        if tokens:
            p = detect_parity(tokens[0])
            if p:
                parity = p
                room, room_err = _parse_room_from_tokens(tokens, 1)
            else:
                room, room_err = _parse_room_from_tokens(tokens, 0)
        else:
            room_err = "Не указана аудитория."
        if room_err:
            return None, room_err
    else:
        return None, "Не указана аудитория после скобок. Пример: ... ) 101 или ... ) н/н 101"

    # нормализуем список недель (по умолчанию до 30-й)
    weeks_list = parse_weeks(weeks_raw, max_week=30)

    lesson = Lesson(
        title=title,
        room=room,                 # уже в каноническом виде: цифры / 'д/о' / 'с/зал'
        weeks_spec=weeks_raw or "1-30",
        weeks=weeks_list,
        parity=parity
    )
    return (group, day, slot, lesson), None

@dp.message(Command("add_lesson"))
async def add_lesson(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        await m.answer("Только для админа.")
        return
    args = (command.args or "").strip()
    parsed, err = parse_add_lesson_args(args)
    if err:
        await m.answer(
            "Формат: /add_lesson <группа> <день> <слот> Название(недели) [н/н|ч/н] Аудитория\n"
            "Примеры:\n"
            "• /add_lesson ИВТ-21 понедельник 2 Алгебра(2-13) н/н 101\n"
            "• /add_lesson ИВТ-21 вторник 1 Физика(2-4,10) 202\n"
            f"Ошибка разбора: {err}"
        )
        return

    group, day, slot, lesson = parsed
    schedules = load_schedules()
    schedules.setdefault(group, {}).setdefault(day, {})
    schedules[group][day][str(slot)] = asdict(lesson)
    save_schedules(schedules)

    par_str = {"odd":"н/н","even":"ч/н",None:"—"}[lesson.parity]
    await m.answer(
        f"✅ Добавлено: **{group}** • {day} • {slot}) {SLOTS[slot]}\n"
        f"{lesson.title} ({lesson.room})\n"
        f"Недели: {lesson.weeks_spec} • Паритет: {par_str}",
        parse_mode="HTML"
    )

@dp.message(Command("del_lesson"))
async def del_lesson(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        await m.answer("Только для админа.")
        return
    args = (command.args or "").strip().split()
    if len(args) < 3:
        await m.answer("Формат: /del_lesson <группа> <день> <слот>")
        return
    group, day, slot_s = args[0], args[1].lower(), args[2]
    if day not in RU_WEEKDAYS:
        await m.answer(f"День должен быть одним из: {', '.join(RU_WEEKDAYS)}"); return
    try:
        slot = int(slot_s)
        if slot not in SLOTS: raise ValueError()
    except ValueError:
        await m.answer("Слот должен быть числом 1..4"); return

    schedules = load_schedules()
    if schedules.get(group, {}).get(day, {}).pop(str(slot), None) is None:
        await m.answer("Такой пары не найдено.")
    else:
        save_schedules(schedules)
        await m.answer("🗑️ Пара удалена.")

@dp.message(Command("show_day"))
async def show_day(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        await m.answer("Только для админа.")
        return
    args = (command.args or "").strip().split()
    if len(args) < 2:
        await m.answer("Формат: /show_day <группа> <день>")
        return
    group, day = args[0], args[1].lower()
    schedules = load_schedules()
    day_data = schedules.get(group, {}).get(day, {})
    if not day_data:
        await m.answer("Нет записей.")
        return
    lines = [f"**{group} — {day.capitalize()}**"]
    for slot in sorted(map(int, day_data.keys())):
        l = Lesson(**day_data[str(slot)])
        par_str = {"odd":"н/н","even":"ч/н",None:"—"}[l.parity]
        lines.append(f"{slot}) {SLOTS[slot]} • {l.title} ({l.room}) • недели: {l.weeks_spec} • {par_str}")
    await m.answer("\n".join(lines), parse_mode="HTML")

# ========= Точка входа =========



app = FastAPI()

@app.on_event("startup")
async def on_startup():
    if not BASE_URL:
        raise RuntimeError("BASE_URL не задан")
    await bot.set_webhook(f"{BASE_URL}{WEBHOOK_PATH}", secret_token=WEBHOOK_SECRET)

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()


@app.get("/")
async def root():
    return {"ok": True, "service": "parahod-bot"}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return {"ok": False}
    data = await request.json()
    update = types.Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}

