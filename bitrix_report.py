#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Отчёты по лидам Битрикс24 -> Telegram.

Три вида отчётов (config `reports`, по умолчанию все):
  day   — за вчера, отправляется ежедневно в send_hour;
  week  — за прошлую календарную неделю (Пн–Вс), отправляется в понедельник;
  month — за прошлый календарный месяц, отправляется 1-го числа.

Как работает автоматика: планировщик (launchd / cron / GitHub Actions) запускает
скрипт каждые 15 минут, а скрипт сам решает, пора ли отправлять — в state.json
он помнит, какие отчёты уже уходили. Поэтому:

  * повторные запуски ничего не дублируют (можно дёргать часто);
  * если в час отправки не было интернета — отчёт уйдёт со следующим запуском;
  * после простоя daily-отчёты досылаются за пропущенные дни (до max_catchup_days);
  * если что-то сломалось — в группу приходит предупреждение (не чаще раза
    в 2 часа), а детали пишутся в report.log рядом со скриптом.

Флаги:
  --dry-run                  показать отчёт в консоли, ничего не отправлять
  --period day|week|month    период для --dry-run; с --force — какой отчёт
                             отправить принудительно (по умолчанию все)
  --force                    отправить прямо сейчас, даже если уже отправлено
  --config PATH              путь к конфигу (по умолчанию config.json рядом со скриптом)

Конфиг читается из config.json, а если файла нет — из переменных окружения
BTR_* (так скрипт работает в GitHub Actions, где секреты нельзя класть в файл).
"""
import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BITRIX_TIMEOUT = 30
TG_TIMEOUT = 30
TG_API = os.environ.get("BTR_TG_API", "https://api.telegram.org")
ERROR_NOTIFY_INTERVAL = 2 * 3600  # предупреждение об ошибке не чаще, чем раз в 2 часа
MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь",
             "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    try:
        with open(SCRIPT_DIR / "report.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr)


def load_config(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    cfg = config_from_env()
    if cfg:
        return cfg
    example = path.with_name("config.example.json")
    sys.exit(f"Не найден конфиг {path} и не заданы переменные BTR_* — "
             f"скопируйте {example.name} в {path.name} и заполните")


def config_from_env() -> dict:
    """Конфиг из переменных окружения — для запуска в CI без файлов с секретами."""
    if not os.environ.get("BTR_BITRIX_WEBHOOK"):
        return None

    def split(name):
        raw = os.environ.get(name, "").strip()
        return [item.strip() for item in raw.split(",") if item.strip()] if raw else []

    try:
        send_hour = int(os.environ.get("BTR_SEND_HOUR", "9"))
        poll_seconds = int(os.environ.get("BTR_POLL_SECONDS") or 0)
    except ValueError:
        send_hour, poll_seconds = 9, 0
    return {
        "bitrix_webhook": os.environ["BTR_BITRIX_WEBHOOK"].strip(),
        "telegram_token": os.environ.get("BTR_TELEGRAM_TOKEN", "").strip(),
        "telegram_chat_id": os.environ.get("BTR_TELEGRAM_CHAT_ID", "").strip(),
        "send_hour": send_hour,
        "poll_seconds": poll_seconds,
        "reports": split("BTR_REPORTS") or ["day", "week", "month"],
        "statuses": split("BTR_STATUSES"),
        "utm_sources": split("BTR_UTM_SOURCES"),
        "breakdown_by_status": os.environ.get("BTR_BREAKDOWN_BY_STATUS", "1").lower() not in ("0", "false"),
        "max_catchup_days": int(os.environ.get("BTR_MAX_CATCHUP_DAYS", "7")),
    }


def http_post_json(url: str, payload: dict, timeout: int) -> dict:
    """POST с JSON-телом; при HTTP-ошибке пытается разобрать JSON из ответа."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            raise err


def call_bitrix(webhook: str, method: str, params: dict) -> dict:
    """Вызов метода REST API с повторами на случай сетевых сбоев и 5xx."""
    url = f"{webhook.rstrip('/')}/{method}"
    last_error = None
    for attempt in range(3):
        try:
            data = http_post_json(url, params, BITRIX_TIMEOUT)
            if "error" in data:
                raise RuntimeError(f"Битрикс24, метод {method}: {data['error']} — "
                                   f"{data.get('error_description', '')}")
            return data
        except (urllib.error.URLError, ValueError) as err:
            last_error = err
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Битрикс24 недоступен ({method}): {last_error}")


def fetch_leads(cfg: dict, date_from: str, date_to: str) -> list:
    """Лиды за период: выбранные статусы × выбранные UTM_SOURCE (если заданы)."""
    webhook = cfg["bitrix_webhook"]
    flt = {">=DATE_CREATE": date_from, "<DATE_CREATE": date_to}
    if cfg.get("statuses"):
        flt["STATUS_ID"] = cfg["statuses"]
    if cfg.get("utm_sources"):
        flt["UTM_SOURCE"] = cfg["utm_sources"]
    fields = ["ID", "STATUS_ID", "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN",
              "UTM_CONTENT", "UTM_TERM"]
    leads, start = [], 0
    while True:
        data = call_bitrix(webhook, "crm.lead.list",
                           {"filter": flt, "select": fields, "start": start})
        page = data.get("result", [])
        leads.extend(page)
        total = int(data.get("total") or 0)
        if not page or len(leads) >= total or len(leads) >= 5000:
            return leads
        start += len(page)


def status_names(webhook: str) -> dict:
    """ID статусов лида -> названия, чтобы отчёт был читаемым."""
    data = call_bitrix(webhook, "crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}})
    names = {}
    for item in data.get("result", []):
        key = item.get("STATUS_ID") or item.get("ID")
        if key:
            names[key] = item.get("NAME", key)
    return names


def count_by(leads: list, field: str) -> dict:
    """Сколько лидов приходится на каждое значение поля, по убыванию."""
    counts = {}
    for lead in leads:
        value = (lead.get(field) or "").strip() or "(без метки)"
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def fmt(value: str, limit: int = 60) -> str:
    if len(value) > limit:
        value = value[:limit] + "…"
    return html.escape(value)


def build_report(cfg: dict, date_from: str, date_to: str, title: str) -> str:
    utm_sources = cfg.get("utm_sources") or []
    leads = fetch_leads(cfg, date_from, date_to)

    lines = [f"📊 <b>Отчёт по лидам {title}</b>"]
    if utm_sources:
        lines.append(f"источники: {fmt(', '.join(utm_sources))}")
    lines.append("")

    if not leads:
        lines.append("Лидов по заданным статусам и источникам не было.")
        return "\n".join(lines)

    for section_title, field in (("По источникам (utm_source)", "UTM_SOURCE"),
                                 ("По каналам (utm_medium)", "UTM_MEDIUM"),
                                 ("По кампаниям (utm_campaign)", "UTM_CAMPAIGN"),
                                 ("По объявлениям (utm_content)", "UTM_CONTENT"),
                                 ("По ключам (utm_term)", "UTM_TERM")):
        counts = count_by(leads, field)
        if set(counts) == {"(без метки)"}:
            continue  # поле пустое у всех лидов — раздел не показываем
        lines.append(f"<b>{section_title}:</b>")
        for value, count in counts.items():
            lines.append(f"• {fmt(value)}: <b>{count}</b>")
        lines.append("")

    if cfg.get("statuses") and cfg.get("breakdown_by_status", True):
        names = status_names(cfg["bitrix_webhook"])
        lines.append("<b>По статусам:</b>")
        for status_id, count in count_by(leads, "STATUS_ID").items():
            lines.append(f"• {fmt(names.get(status_id, status_id))}: <b>{count}</b>")
        lines.append("")

    lines.append(f"Итого: <b>{len(leads)}</b>")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"{TG_API}/bot{token}/sendMessage"
    data = http_post_json(url, {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                          TG_TIMEOUT)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API: {data.get('description', 'неизвестная ошибка')} "
                           f"(проверьте chat_id и права бота в группе)")


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # нет постоянного диска (CI) — не должно ломать отправку отчёта


def state_path_for(cfg: dict) -> Path:
    return Path(cfg.get("state_file") or (SCRIPT_DIR / "state.json"))


def completed_periods(now: datetime) -> dict:
    """Завершённые периоды, по которым можно отчитаться: вчера, прошлая неделя, прошлый месяц."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = today - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday() + 7)  # понедельник прошлой недели
    month_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    return {
        "day": {"start": day_start, "end": today,
                "title": f"за {day_start:%d.%m.%Y}", "key": day_start.strftime("%Y-%m-%d")},
        "week": {"start": week_start, "end": week_start + timedelta(days=7),
                 "title": f"за неделю {week_start:%d.%m}–{week_start + timedelta(days=6):%d.%m.%Y}",
                 "key": week_start.strftime("%Y-%m-%d")},
        "month": {"start": month_start, "end": today.replace(day=1),
                  "title": f"за {MONTHS_RU[month_start.month - 1]} {month_start:%Y}",
                  "key": month_start.strftime("%Y-%m")},
    }


def pending_reports(cfg: dict, state: dict, force: bool, only: str = None) -> list:
    """Отчёты, которые пора отправить: [{kind, start, end, title, key}, ...]."""
    now = datetime.now()
    periods = completed_periods(now)
    enabled = cfg.get("reports") or ["day", "week", "month"]
    on_time = force or now.hour >= int(cfg.get("send_hour", 9))
    sent = state.get("sent") or {}
    if not sent.get("day") and state.get("last_report_date"):
        sent["day"] = state["last_report_date"]  # state.json старого формата

    out = []
    for kind in ("day", "week", "month"):
        if kind not in enabled or (only and kind != only):
            continue
        period = periods[kind]

        if kind == "day":
            # с догоном пропущенных дней: от последнего отправленного до вчера
            try:
                last_sent = datetime.strptime(sent.get("day") or "", "%Y-%m-%d")
            except ValueError:
                last_sent = period["start"] - timedelta(days=1)
            if force:
                last_sent = period["start"] - timedelta(days=1)
            days, day = [], last_sent + timedelta(days=1)
            while day <= period["start"]:
                days.append(day)
                day += timedelta(days=1)
            max_catchup = int(cfg.get("max_catchup_days", 7))
            if max_catchup > 0:
                days = days[-max_catchup:]
            if days and on_time:
                for d in days:
                    out.append({"kind": "day", "start": d, "end": d + timedelta(days=1),
                                "title": f"за {d:%d.%m.%Y}", "key": d.strftime("%Y-%m-%d")})
        elif sent.get(kind) != period["key"] and on_time:
            out.append({"kind": kind, "start": period["start"], "end": period["end"],
                        "title": period["title"], "key": period["key"]})
    return out


def run_scheduled(cfg: dict, force: bool = False, only: str = None) -> None:
    spath = state_path_for(cfg)
    state = load_state(spath)
    reports = pending_reports(cfg, state, force, only)
    if not reports:
        return  # не время либо всё уже отправлено — запуск-проверка ничего не делает
    for rep in reports:
        date_from = rep["start"].strftime("%Y-%m-%d %H:%M:%S")
        date_to = rep["end"].strftime("%Y-%m-%d %H:%M:%S")
        text = build_report(cfg, date_from, date_to, rep["title"])
        send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], text)
        state.setdefault("sent", {})[rep["kind"]] = rep["key"]
        save_state(spath, state)
        log(f"отчёт {rep['title']} отправлен в группу {cfg['telegram_chat_id']}")


def notify_error(cfg: dict, error: Exception) -> None:
    """Предупреждение в группу, что отчёт не ушёл (с троттлингом, чтобы не спамить)."""
    spath = state_path_for(cfg)
    state = load_state(spath)
    if time.time() - (state.get("last_error_ts") or 0) < ERROR_NOTIFY_INTERVAL:
        return
    try:
        send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"],
                      "⚠️ <b>Отчёт по лидам не отправлен</b>\n"
                      f"<code>{html.escape(str(error)[:800])}</code>\n"
                      "Следующая попытка — в ближайший запуск по расписанию.")
        state["last_error_ts"] = time.time()
        save_state(spath, state)
    except Exception:
        pass  # недоступен и Telegram — причина останется в report.log


# ----------------------------- команды бота -----------------------------

HELP_TEXT = (
    "🤖 <b>Команды отчётов по лидам</b>\n"
    "/day — отчёт за вчера\n"
    "/day 14.08 — за конкретный день (можно 14.08.2026 или 2026-08-14)\n"
    "/week — за прошлую неделю (Пн–Вс)\n"
    "/week 12.08 — неделя, в которую попадает дата\n"
    "/month — за прошлый месяц\n"
    "/month 07.2026 — за конкретный месяц\n"
    "/range 10.08 16.08 — произвольный период (макс 92 дня)\n"
    "/help — эта справка"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_day(raw: str, today: datetime):
    """«14.08», «14.08.2026», «2026-08-14», «вчера», «сегодня» -> дата или None."""
    s = (raw or "").strip().lower()
    if s in ("вчера", "yesterday"):
        return today - timedelta(days=1)
    if s in ("сегодня", "today"):
        return today
    for pattern in ("%d.%m.%Y", "%d.%m", "%Y-%m-%d", "%d"):
        try:
            d = datetime.strptime(s, pattern)
        except ValueError:
            continue
        if "%Y" not in pattern:
            d = d.replace(year=today.year)
        if "%m" not in pattern:
            d = d.replace(month=today.month)
        return d
    return None


def month_bounds(month_start: datetime):
    end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return month_start, end


def process_command(cfg: dict, text: str):
    """Ответ на команду из чата. None — молча проигнорировать (не команда)."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]
    periods = completed_periods(datetime.now())

    if cmd in ("/start", "/help"):
        return HELP_TEXT

    if cmd == "/day":
        day = parse_day(args[0] if args else "вчера", today)
        if day is None:
            return "⚠️ Не понял дату. Примеры: /day 14.08 или /day 14.08.2026"
        return build_report(cfg, day.strftime(DATE_FORMAT),
                            (day + timedelta(days=1)).strftime(DATE_FORMAT),
                            f"за {day:%d.%m.%Y}")

    if cmd == "/week":
        if args:
            ref = parse_day(args[0], today)
            if ref is None:
                return "⚠️ Не понял дату. Пример: /week 12.08"
        else:
            ref = periods["week"]["start"] + timedelta(days=3)  # середина прошлой недели
        monday = ref - timedelta(days=ref.weekday())
        return build_report(cfg, monday.strftime(DATE_FORMAT),
                            (monday + timedelta(days=7)).strftime(DATE_FORMAT),
                            f"за неделю {monday:%d.%m}–{monday + timedelta(days=6):%d.%m.%Y}")

    if cmd == "/month":
        if args:
            month = None
            for pattern in ("%m.%Y", "%d.%m.%Y", "%d.%m", "%Y-%m-%d"):
                try:
                    month = datetime.strptime(args[0], pattern).replace(day=1)
                    if "%Y" not in pattern:
                        month = month.replace(year=today.year)
                    break
                except ValueError:
                    continue
            if month is None:
                return "⚠️ Не понял месяц. Пример: /month 07.2026"
        else:
            month = periods["month"]["start"]
        start, end = month_bounds(month)
        return build_report(cfg, start.strftime(DATE_FORMAT), end.strftime(DATE_FORMAT),
                            f"за {MONTHS_RU[start.month - 1]} {start:%Y}")

    if cmd == "/range":
        if len(args) < 2:
            return "⚠️ Нужно две даты. Пример: /range 10.08 16.08"
        d1, d2 = parse_day(args[0], today), parse_day(args[1], today)
        if d1 is None or d2 is None:
            return "⚠️ Не понял даты. Пример: /range 10.08 16.08"
        if d1 > d2:
            d1, d2 = d2, d1
        if (d2 - d1).days > 92:
            return "⚠️ Период слишком длинный, максимум 92 дня."
        return build_report(cfg, d1.strftime(DATE_FORMAT),
                            (d2 + timedelta(days=1)).strftime(DATE_FORMAT),
                            f"за период {d1:%d.%m}–{d2:%d.%m.%Y}")

    if cmd.startswith("/"):
        return f"Не знаю команду {cmd}\n\n{HELP_TEXT}"
    return None


def handle_commands(cfg: dict, poll_seconds: int) -> None:
    """Читает команды из Telegram (getUpdates) и отвечает отчётами.

    poll_seconds > 0 — слушать непрерывно столько секунд (long polling),
    0 — одна короткая проверка накопившихся команд.
    Смещение update_id хранится в state.json, поэтому команды не теряются
    и не обрабатываются дважды между запусками.
    """
    spath = state_path_for(cfg)
    state = load_state(spath)
    chat_id = str(cfg.get("telegram_chat_id", ""))
    url = f"{TG_API}/bot{cfg['telegram_token']}/getUpdates"
    offset = state.get("tg_offset") or 0
    single_pass = poll_seconds <= 0
    deadline = time.time() + poll_seconds

    while True:
        remaining = deadline - time.time()
        if not single_pass and remaining <= 0:
            break
        timeout = 0 if single_pass else min(25, max(1, int(remaining)))
        try:
            data = http_post_json(url, {"timeout": timeout, "offset": offset,
                                        "allowed_updates": ["message"]}, timeout + 15)
        except Exception as err:
            log(f"getUpdates: {err}")
            if single_pass:
                break
            time.sleep(5)
            continue
        if not data.get("ok"):
            log(f"getUpdates: {data.get('description', 'ошибка')}")
            if not single_pass:
                time.sleep(5)
            continue

        for update in data.get("result", []):
            offset = max(offset, update["update_id"] + 1)
            state["tg_offset"] = offset
            save_state(spath, state)
            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            if not text or str(message.get("chat", {}).get("id", "")) != chat_id:
                continue
            try:
                reply = process_command(cfg, text)
            except Exception as err:
                reply = f"⚠️ Не удалось собрать отчёт: {html.escape(str(err)[:400])}"
            if reply is None:
                continue
            try:
                send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], reply)
                log(f"команда «{text}» выполнена")
            except Exception as err:
                log(f"не удалось ответить на «{text}»: {err}")
        if single_pass:
            break


def main():
    parser = argparse.ArgumentParser(description="Отчёты по лидам Битрикс24 в Telegram")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.json"),
                        help="путь к config.json (по умолчанию — рядом со скриптом)")
    parser.add_argument("--period", choices=["day", "week", "month", "yesterday", "today"],
                        help="day/yesterday, week, month; today — только для --dry-run")
    parser.add_argument("--force", action="store_true",
                        help="отправить сейчас, даже если уже отправлено "
                             "(с --period — только указанный отчёт)")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать отчёт в консоли, ничего не отправляя")
    args = parser.parse_args()

    cfg = None
    try:
        cfg = load_config(Path(args.config))
        for key in ("bitrix_webhook", "telegram_token", "telegram_chat_id"):
            if not cfg.get(key):
                raise RuntimeError(f"не заполнено поле {key} (config.json или переменные BTR_*)")

        if args.dry_run:
            now = datetime.now()
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if args.period == "today":
                start, end, title = today, today + timedelta(days=1), f"за {today:%d.%m.%Y} (сегодня)"
            else:
                period = completed_periods(now)[args.period or "day"]
                start, end, title = period["start"], period["end"], period["title"]
            print(build_report(cfg, start.strftime("%Y-%m-%d %H:%M:%S"),
                               end.strftime("%Y-%m-%d %H:%M:%S"), title))
            return

        only = args.period if args.period in ("day", "week", "month") else None
        run_scheduled(cfg, args.force, only)
        try:
            handle_commands(cfg, int(cfg.get("poll_seconds") or 0))
        except Exception as err:
            log(f"ОШИБКА (команды бота): {err}")
    except Exception as err:
        log(f"ОШИБКА: {type(err).__name__}: {err}")
        if cfg is not None:
            notify_error(cfg, err)
        sys.exit(1)


if __name__ == "__main__":
    main()
