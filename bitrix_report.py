#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневный отчёт по лидам Битрикс24 -> Telegram.

Как работает автоматика: планировщик (launchd / cron / GitHub Actions) запускает
скрипт каждые 15 минут, а скрипт сам решает, пора ли отправлять отчёт —
в state.json он помнит, за какую дату отчёт уже ушёл. Поэтому:

  * повторные запуски ничего не дублируют (можно дёргать часто);
  * если в час отправки не было интернета — отчёт уйдёт со следующим запуском;
  * после простоя (ноутбук выключали, GitHub лежал) отправляются все
    пропущенные дни, до max_catchup_days за раз;
  * если что-то сломалось (Битрикс недоступен, неверный конфиг) — в группу
    приходит предупреждение об ошибке (не чаще раза в 2 часа), а детали
    пишутся в report.log рядом со скриптом.

Флаги:
  --dry-run                  показать отчёт в консоли, ничего не отправлять
  --period yesterday|today   за какой день (по умолчанию — из конфига)
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
    except ValueError:
        send_hour = 9
    return {
        "bitrix_webhook": os.environ["BTR_BITRIX_WEBHOOK"].strip(),
        "telegram_token": os.environ.get("BTR_TELEGRAM_TOKEN", "").strip(),
        "telegram_chat_id": os.environ.get("BTR_TELEGRAM_CHAT_ID", "").strip(),
        "period": os.environ.get("BTR_PERIOD", "yesterday"),
        "send_hour": send_hour,
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
    fields = ["ID", "STATUS_ID", "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN"]
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


def status_names(webhook: str) -> dict:
    """ID статусов лида -> названия, чтобы отчёт был читаемым."""
    data = call_bitrix(webhook, "crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}})
    names = {}
    for item in data.get("result", []):
        key = item.get("STATUS_ID") or item.get("ID")
        if key:
            names[key] = item.get("NAME", key)
    return names


def build_report(cfg: dict, day: datetime) -> str:
    statuses = cfg.get("statuses") or []
    utm_sources = cfg.get("utm_sources") or []
    # API Битрикс24 понимает даты фильтра в часовом поясе портала
    date_from = day.strftime("%Y-%m-%d %H:%M:%S")
    date_to = (day + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    leads = fetch_leads(cfg, date_from, date_to)

    lines = [f"📊 <b>Отчёт по лидам за {day:%d.%m.%Y}</b>"]
    if utm_sources:
        lines.append(f"источники: {fmt(', '.join(utm_sources))}")
    lines.append("")

    if not leads:
        lines.append("Лидов по заданным статусам и источникам не было.")
        return "\n".join(lines)

    for title, field in (("По источникам (utm_source)", "UTM_SOURCE"),
                         ("По каналам (utm_medium)", "UTM_MEDIUM"),
                         ("По кампаниям (utm_campaign)", "UTM_CAMPAIGN")):
        lines.append(f"<b>{title}:</b>")
        for value, count in count_by(leads, field).items():
            lines.append(f"• {fmt(value)}: <b>{count}</b>")
        lines.append("")

    if statuses and cfg.get("breakdown_by_status", True):
        names = status_names(cfg["bitrix_webhook"])
        lines.append("<b>По статусам:</b>")
        for status_id, count in count_by(leads, "STATUS_ID").items():
            lines.append(f"• {fmt(names.get(status_id, status_id))}: <b>{count}</b>")
        lines.append("")

    lines.append(f"Итого за день: <b>{len(leads)}</b>")
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


def pending_days(cfg: dict, state: dict, force: bool, period: str):
    """Дни, по которым отчёт ещё не отправлялся (для period=yesterday — с догоном)."""
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    send_hour = int(cfg.get("send_hour", 9))

    if period == "today":
        already_sent = state.get("last_report_date") == today.strftime("%Y-%m-%d")
        if already_sent or (not force and now.hour < send_hour):
            return []
        return [today]

    try:
        last_sent = datetime.strptime(state.get("last_report_date") or "", "%Y-%m-%d")
    except ValueError:
        last_sent = yesterday - timedelta(days=1)  # самый первый запуск — только вчера
    if force:
        last_sent = yesterday - timedelta(days=1)

    days = []
    day = last_sent + timedelta(days=1)
    while day <= yesterday:
        days.append(day)
        day += timedelta(days=1)
    max_catchup = int(cfg.get("max_catchup_days", 7))
    if max_catchup > 0:
        days = days[-max_catchup:]
    if days and not force and now.hour < send_hour:
        return []  # ещё не наступило время отправки — тихо выходим
    return days


def run_scheduled(cfg: dict, force: bool, period: str) -> None:
    spath = state_path_for(cfg)
    state = load_state(spath)
    days = pending_days(cfg, state, force, period)
    if not days:
        return  # не время либо отчёт уже ушёл — запуск-проверка ничего не делает
    for day in days:
        report = build_report(cfg, day)
        send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], report)
        state["last_report_date"] = day.strftime("%Y-%m-%d")
        save_state(spath, state)
        log(f"отчёт за {day:%d.%m.%Y} отправлен в группу {cfg['telegram_chat_id']}")


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


def main():
    parser = argparse.ArgumentParser(description="Отчёт по лидам Битрикс24 в Telegram")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.json"),
                        help="путь к config.json (по умолчанию — рядом со скриптом)")
    parser.add_argument("--period", choices=["yesterday", "today"],
                        help="за какой день отчёт (переопределяет config.json)")
    parser.add_argument("--force", action="store_true",
                        help="отправить сейчас, даже если за этот день уже отправлено")
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
            day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            if (args.period or cfg.get("period", "yesterday")) == "yesterday":
                day -= timedelta(days=1)
            print(build_report(cfg, day))
            return

        run_scheduled(cfg, args.force, args.period or cfg.get("period", "yesterday"))
    except Exception as err:
        log(f"ОШИБКА: {type(err).__name__}: {err}")
        if cfg is not None:
            notify_error(cfg, err)
        sys.exit(1)


if __name__ == "__main__":
    main()
