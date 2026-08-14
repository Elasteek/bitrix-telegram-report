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
import re
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
        # список chat_id через запятую — отчёты уходят во все, команды работают в каждом
        "telegram_chat_id": [c.strip() for c in os.environ.get("BTR_TELEGRAM_CHAT_ID", "").split(",") if c.strip()],
        # чаты только для АВТООТЧЁТОВ; пусто = все telegram_chat_id
        "report_chat_id": split("BTR_REPORT_CHAT_ID"),
        # чаты в «чистом» режиме — без строк «(без метки)» в отчётах
        "clean_chats": split("BTR_CLEAN_CHATS"),
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


def fetch_leads(cfg: dict, date_from: str, date_to: str, extra: dict = None) -> list:
    """Лиды за период: выбранные статусы × выбранные UTM_SOURCE (если заданы).

    extra — дополнительный фильтр вида {"UTM_SOURCE": "ig"} (кнопки меню).
    """
    webhook = cfg["bitrix_webhook"]
    flt = {">=DATE_CREATE": date_from, "<DATE_CREATE": date_to}
    if cfg.get("statuses"):
        flt["STATUS_ID"] = cfg["statuses"]
    if cfg.get("utm_sources"):
        flt["UTM_SOURCE"] = cfg["utm_sources"]
    if extra:
        flt.update(extra)
    fields = ["ID", "STATUS_ID", "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN",
              "UTM_CONTENT", "UTM_TERM", "UF_CRM_PRODUCT", "PHONE"]
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


# телефонные коды стран для определения географии по номеру лида
COUNTRY_CODES = {
    "1": "США/Канада", "20": "Египет", "27": "ЮАР", "31": "Нидерланды", "32": "Бельгия",
    "33": "Франция", "34": "Испания", "36": "Венгрия", "39": "Италия", "40": "Румыния",
    "41": "Швейцария", "43": "Австрия", "44": "Великобритания", "45": "Дания",
    "46": "Швеция", "47": "Норвегия", "48": "Польша", "49": "Германия",
    "51": "Перу", "52": "Мексика", "53": "Куба", "54": "Аргентина", "55": "Бразилия",
    "56": "Чили", "57": "Колумбия", "60": "Малайзия", "61": "Австралия",
    "62": "Индонезия", "63": "Филиппины", "64": "Новая Зеландия", "65": "Сингапур",
    "66": "Таиланд", "81": "Япония", "82": "Южная Корея", "84": "Вьетнам",
    "86": "Китай", "90": "Турция", "91": "Индия", "92": "Пакистан", "93": "Афганистан",
    "94": "Шри-Ланка", "98": "Иран", "211": "Южный Судан", "212": "Марокко",
    "213": "Алжир", "216": "Тунис", "218": "Либия", "220": "Гамбия", "233": "Гана",
    "234": "Нигерия", "249": "Судан", "250": "Руанда", "251": "Эфиопия",
    "254": "Кения", "255": "Танзания", "256": "Уганда", "260": "Замбия",
    "263": "Зимбабве", "267": "Ботсвана", "351": "Португалия", "352": "Люксембург",
    "355": "Албания", "356": "Мальта", "357": "Кипр", "358": "Финляндия",
    "359": "Болгария", "370": "Литва", "371": "Латвия", "372": "Эстония",
    "373": "Молдова", "374": "Армения", "375": "Беларусь", "376": "Андорра",
    "377": "Монако", "378": "Сан-Марино", "380": "Украина", "381": "Сербия",
    "382": "Черногория", "383": "Косово", "385": "Хорватия", "386": "Словения",
    "387": "Босния и Герцеговина", "389": "Северная Македония", "420": "Чехия",
    "421": "Словакия", "423": "Лихтенштейн", "501": "Белиз", "502": "Гватемала",
    "503": "Сальвадор", "504": "Гондурас", "505": "Никарагуа", "506": "Коста-Рика",
    "507": "Панама", "509": "Гаити", "590": "Гваделупа", "591": "Боливия",
    "593": "Эквадор", "595": "Парагвай", "598": "Уругвай", "670": "Восточный Тимор",
    "672": "Австралия", "673": "Бруней", "675": "Папуа — Новая Гвинея",
    "676": "Тонга", "679": "Фиджи", "7": "Россия", "800": "Бесплатный номер",
    "808": "Бесплатный номер", "850": "КНДР", "852": "Гонконг", "853": "Макао",
    "855": "Камбоджа", "856": "Лаос", "880": "Бангладеш", "886": "Тайвань",
    "960": "Мальдивы", "961": "Ливан", "962": "Иордания", "963": "Сирия",
    "964": "Ирак", "965": "Кувейт", "966": "Саудовская Аравия", "967": "Йемен",
    "968": "Оман", "970": "Палестина", "971": "ОАЭ", "972": "Израиль",
    "973": "Бахрейн", "974": "Катар", "975": "Бутан", "976": "Монголия",
    "977": "Непал", "992": "Таджикистан", "993": "Туркменистан", "994": "Азербайджан",
    "995": "Грузия", "996": "Кыргызстан", "998": "Узбекистан",
}


def phone_country(phone) -> str:
    """Страна по номеру телефона (по префиксу кода страны, 8 -> 7)."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    # +7 6xx/7xx — Казахстан, остальное +7 — Россия
    if digits.startswith("7") and len(digits) >= 2 and digits[1] in "67":
        return "Казахстан"
    for size in (3, 2, 1):
        if digits[:size] in COUNTRY_CODES:
            return COUNTRY_CODES[digits[:size]]
    return ""


def count_countries(leads: list) -> dict:
    """Сколько лидов из какой страны (по первому телефону лида)."""
    counts = {}
    for lead in leads:
        phones = lead.get("PHONE") or []
        value = phones[0].get("VALUE") if phones and isinstance(phones[0], dict) else None
        country = phone_country(value) or "не определена"
        counts[country] = counts.get(country, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def strip_product_suffix(name: str) -> str:
    """Отрезает «приветственные» суффиксы: «Музыкальный продюсер Бесплатно» и
    «… Бесплатная часть» — один бесплатный курс с разных страниц.
    Доп. слова — через переменную окружения BTR_PRODUCT_STRIP (через запятую)."""
    markers = ["бесплатная часть", "бесплатный урок", "бесплатно", "вводный урок"]
    extra = [m.strip() for m in (os.environ.get("BTR_PRODUCT_STRIP") or "").split(",")
             if m.strip()]
    pattern = re.compile(r"[\s\-–]*(?:" + "|".join(re.escape(m) for m in markers + extra)
                         + r")\s*$", re.IGNORECASE)
    for _ in range(2):
        stripped = pattern.sub("", name).strip(" \t-–")
        if stripped == name:
            break
        name = stripped
    return name


PRICE_TAIL = re.compile(r"\s*-\s*(\d+)\s*x\s*(\d+)(?:\s*=\s*[\d\s]+)?\s*$", re.IGNORECASE)
MONTHLY_TAIL = re.compile(r"\s*от\s+[\d\s.,]+\s*/\s*мес.*$", re.IGNORECASE)
FREE_WORDS = re.compile(r"бесплатн|вводный урок", re.IGNORECASE)


def normalize_product(part: str):
    """-> (каноничное имя, бесплатный ли). Бесплатные варианты курса склеиваются,
    платные («Тариф …») живут отдельно. Бесплатность — по цене в хвосте «1x0»
    либо по слову «бесплатно/вводный урок»."""
    name = part.strip()
    m = PRICE_TAIL.search(name)
    price = int(m.group(2)) if m else None
    if m:
        name = name[:m.start()].strip()
    name = MONTHLY_TAIL.sub("", name)                                  # «от 1 875 /мес…»
    name = re.sub(r"^(?:видео)?курс[:\s]\s*", "", name, flags=re.IGNORECASE)  # «Курс: »
    name = re.sub(r"\s+", " ", name).strip(" \t-–«»\"'")
    is_free = (price == 0 or (price is None and bool(FREE_WORDS.search(name)))) \
        and not re.search(r"\bтариф\b", name, re.IGNORECASE)
    if is_free:
        name = strip_product_suffix(name)
    return name, bool(is_free and name)


def user_key(lead: dict) -> str:
    """Ключ пользователя для дедупликации: нормализованный телефон, иначе ID лида."""
    digits = ""
    for phone in lead.get("PHONE") or []:
        value = phone.get("VALUE") if isinstance(phone, dict) else None
        digits = re.sub(r"\D", "", str(value or ""))
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if digits:
            break
    return digits or f"lead-{lead.get('ID')}"


def count_products(leads: list) -> tuple:
    """-> (бесплатные, платные): {курс: сколько раз брали}, каждый по убыванию.

    Бесплатные считаем по лидам (охват). Платные («Попытка оплатить») — по
    уникальным людям: дубли одного пользователя за период (несколько лидов
    с одним телефоном) объединяются в одну попытку."""
    free = {}
    paid_by_user = {}
    for lead in leads:
        raw = lead.get("UF_CRM_PRODUCT") or []
        items = raw if isinstance(raw, list) else [raw]
        entries = set()
        for item in items:
            for part in str(item).split(";"):
                name, is_free = normalize_product(part)
                if name:
                    entries.add((name, is_free))
        for name, is_free in entries:
            if is_free:
                free[name] = free.get(name, 0) + 1
        paid_names = {name for name, is_free in entries if not is_free}
        if paid_names:
            key = user_key(lead)
            paid_by_user.setdefault(key, set()).update(paid_names)
    paid = {}
    for names in paid_by_user.values():
        for name in names:
            paid[name] = paid.get(name, 0) + 1
    by_count = lambda d: dict(sorted(d.items(), key=lambda item: -item[1]))
    return by_count(free), by_count(paid)


def plural_times(n: int) -> str:
    """1 раз / 2 раза / 5 раз."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} раз"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} раза"
    return f"{n} раз"


def build_summary(leads: list, clean: bool = False) -> list:
    """Итог по меткам: сколько лидов у каждого источника, от большего к меньшему."""
    groups = {}
    for lead in leads:
        source = (lead.get("UTM_SOURCE") or "").strip() or "(без метки)"
        groups[source] = groups.get(source, 0) + 1
    ordered = [(s, n) for s, n in sorted(groups.items(), key=lambda item: -item[1])
               if not (clean and s == "(без метки)")]
    lines = [f"• <b>{count}</b> — {fmt(source)}" for source, count in ordered[:7]]
    if len(ordered) > 7:
        lines.append(f"• …и ещё {len(ordered) - 7} источников")
    return lines


def fmt(value: str, limit: int = 60) -> str:
    if len(value) > limit:
        value = value[:limit] + "…"
    return html.escape(value)


def build_report(cfg: dict, date_from: str, date_to: str, title: str,
                 extra: dict = None, clean: bool = False) -> str:
    """Отчёт за период. clean=True — «чистый» режим: без строк «(без метки)»."""
    utm_sources = cfg.get("utm_sources") or []
    leads = fetch_leads(cfg, date_from, date_to, extra)

    lines = [f"📊 <b>Отчёт по лидам {title}</b>"]
    if utm_sources:
        lines.append(f"источники: {fmt(', '.join(utm_sources))}")
    if extra:
        field, value = next(iter(extra.items()))
        lines.append(f"фильтр: {FIELD_LABELS.get(field, field)} = {fmt(value)}")
    lines.append("")

    if not leads:
        lines.append("Лидов по заданным статусам и источникам не было.")
        return "\n".join(lines)

    for section_title, field in (("По источникам (utm_source)", "UTM_SOURCE"),
                                 ("По каналам (utm_medium)", "UTM_MEDIUM"),
                                 ("По кампаниям (utm_campaign)", "UTM_CAMPAIGN"),
                                 ("По объявлениям (utm_content)", "UTM_CONTENT"),
                                 ("По ключам (utm_term)", "UTM_TERM")):
        rows = [(value, count) for value, count in count_by(leads, field).items()
                if not (clean and value == "(без метки)")]
        if not rows:
            continue  # ничего значимого (или всё без меток в чистом режиме)
        lines.append(f"<b>{section_title}:</b>")
        for value, count in rows:
            lines.append(f"• {fmt(value)}: <b>{count}</b>")
        lines.append("")

    if cfg.get("statuses") and cfg.get("breakdown_by_status", True):
        names = status_names(cfg["bitrix_webhook"])
        lines.append("<b>По статусам:</b>")
        for status_id, count in count_by(leads, "STATUS_ID").items():
            lines.append(f"• {fmt(names.get(status_id, status_id))}: <b>{count}</b>")
        lines.append("")

    countries = [(country, count) for country, count in count_countries(leads).items()
                 if not (clean and country == "не определена")]
    if not (len(countries) == 1 and countries[0][0] == "не определена"):
        lines.append("<b>🌍 Страны (по номеру телефона):</b>")
        for country, count in countries:
            lines.append(f"• {fmt(country)}: <b>{count}</b>")
        lines.append("")

    lines.append(f"📋 <b>Итог по меткам: {len(leads)}</b>")
    lines.extend(build_summary(leads, clean))

    free_products, paid_products = count_products(leads)
    if free_products:
        lines.append("")
        lines.append("<b>🎁 Бесплатные уроки (от большего к меньшему):</b>")
        for name, count in list(free_products.items())[:15]:
            lines.append(f"• {fmt(name, 60)} — {plural_times(count)}")
    if paid_products:
        lines.append("")
        lines.append("<b>💳 Попытка оплатить (от большего к меньшему):</b>")
        for name, count in list(paid_products.items())[:10]:
            lines.append(f"• {fmt(name, 60)} — {plural_times(count)}")
    return "\n".join(lines)


def chat_ids(cfg: dict) -> list:
    """Список подключённых chat_id (в конфиге может быть строка или список)."""
    raw = cfg.get("telegram_chat_id") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(c).strip() for c in raw if str(c).strip()]


def report_chat_ids(cfg: dict) -> list:
    """Чаты для автоматических отчётов; если не заданы — все подключённые."""
    raw = cfg.get("report_chat_id") or []
    if isinstance(raw, str):
        raw = [raw]
    ids = [str(c).strip() for c in raw if str(c).strip()]
    return ids or chat_ids(cfg)


def clean_mode(cfg: dict, chat_id) -> bool:
    """«Чистый» режим для чата: в отчётах нет строк «(без метки)»."""
    raw = cfg.get("clean_chats") or []
    if isinstance(raw, str):
        raw = [raw]
    return str(chat_id) in {str(c).strip() for c in raw}


def send_to_all(cfg: dict, text: str, reply_markup: dict = None, chats: list = None) -> None:
    """Отправить сообщение в чаты (по умолчанию во все подключённые);
    сбой одного чата не мешает другим."""
    chats = chats or chat_ids(cfg)
    errors = []
    for chat in chats:
        try:
            send_telegram(cfg["telegram_token"], chat, text, reply_markup)
        except Exception as err:
            errors.append(f"{chat}: {err}")
            log(f"не доставлено в чат {chat}: {err}")
    if errors and len(errors) == len(chats):
        raise RuntimeError("отчёт не доставлен ни в один чат: " + "; ".join(errors))


def send_telegram(token: str, chat_id: str, text: str, reply_markup: dict = None) -> None:
    url = f"{TG_API}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = http_post_json(url, payload, TG_TIMEOUT)
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
        rchats = report_chat_ids(cfg)
        # для «чистых» чатов отчёт собирается без строк «(без метки)»
        plain = [c for c in rchats if not clean_mode(cfg, c)]
        neat = [c for c in rchats if clean_mode(cfg, c)]
        if plain:
            send_to_all(cfg, build_report(cfg, date_from, date_to, rep["title"]),
                        chats=plain)
        if neat:
            send_to_all(cfg, build_report(cfg, date_from, date_to, rep["title"], clean=True),
                        chats=neat)
        state.setdefault("sent", {})[rep["kind"]] = rep["key"]
        save_state(spath, state)
        log(f"отчёт {rep['title']} отправлен ({len(rchats)} чат., из них чистых: {len(neat)})")


def notify_error(cfg: dict, error: Exception) -> None:
    """Предупреждение в группу, что отчёт не ушёл (с троттлингом, чтобы не спамить)."""
    spath = state_path_for(cfg)
    state = load_state(spath)
    if time.time() - (state.get("last_error_ts") or 0) < ERROR_NOTIFY_INTERVAL:
        return
    try:
        send_to_all(cfg,
                    "⚠️ <b>Отчёт по лидам не отправлен</b>\n"
                    f"<code>{html.escape(str(error)[:800])}</code>\n"
                    "Следующая попытка — в ближайший запуск по расписанию.",
                    chats=report_chat_ids(cfg))
        state["last_error_ts"] = time.time()
        save_state(spath, state)
    except Exception:
        pass  # недоступен и Telegram — причина останется в report.log


# ----------------------------- команды бота -----------------------------

HELP_TEXT = (
    "🤖 <b>Отчёты по лидам Битрикс24 — инструкция</b>\n"
    "\n"
    "🔘 <b>КОНСТРУКТОР ПО КНОПКАМ (ничего печатать не надо)</b>\n"
    "/report → период (вчера, сегодня, эта/прошлая неделя, этот/прошлый месяц) → "
    "весь отчёт или рекламное поле → значение\n"
    "Просто отправьте команду и нажимайте кнопки.\n"
    "\n"
    "📅 <b>ОТЧЁТЫ ЗА ПЕРИОД</b>\n"
    "<code>/day</code> — за вчера\n"
    "<code>/day 14.08</code> — за конкретный день (или 14.08.2026, 2026-08-14)\n"
    "<code>/week</code> — прошлая неделя (Пн–Вс)\n"
    "<code>/week 12.08</code> — неделя, в которую попадает дата\n"
    "<code>/month</code> — прошлый месяц\n"
    "<code>/month 07.2026</code> — конкретный месяц\n"
    "<code>/range 10.08 16.08</code> — свой период (до 92 дней)\n"
    "\n"
    "🔎 <b>РЕКЛАМНЫЕ РАЗБИВКИ — меню с кнопками</b>\n"
    "<code>/utmsource</code> — источники за вчера\n"
    "<code>/utmsource 14.08</code> — источники за 14 августа\n"
    "<code>/utmcampaign week</code> — кампании за прошлую неделю\n"
    "<code>/utmterm month</code> — ключи за прошлый месяц\n"
    "Так же работают <code>/utmmedium</code>, <code>/utmcontent</code>.\n"
    "Кнопка выглядит как «значение · число лидов». Нажали — пришёл отчёт только "
    "по этому значению. Меню остаётся — можно нажать несколько и сравнить.\n"
    "\n"
    "⚡ <b>КОРОТКАЯ ФОРМА</b> — к любой команде периода допишите <code>utm поле</code>:\n"
    "<code>/day 14.08 utm source</code>\n"
    "<code>/week utm medium</code>\n"
    "<code>/range 10.08 16.08 utm campaign</code>\n"
    "\n"
    "📚 <b>ПОЛЯ</b>: source — источник · medium — канал · campaign — кампания · "
    "content — объявление · term — ключ. В каждом отчёте все UTM-разделы по "
    "фактическим значениям (пустые скрываются) и список уроков/курсов, "
    "которые брали лиды.\n"
    "\n"
    "ℹ️ <b>ПОЛЕЗНОЕ</b>\n"
    "• Даты — как удобно: 14.08, 14.08.2026, 2026-08-14, «вчера», «сегодня».\n"
    "• Команда из меню «/» улетает сразу, без аргументов — для дат печатайте "
    "текстом (примеры выше) или жмите /report.\n"
    "• Ответ обычно в течение 1–3 минут.\n"
    "• Утренние автоотчёты (в группах, где они включены): за день — в 9:00, "
    "за неделю — в понедельник, за месяц — 1-го числа.\n"
    "• Считаются лиды в статусах: Новый, Прогрев, Попытка оплатить курс, "
    "Диалог с куратором, Диагностика, Конвертирован.\n"
    "• Если что-то сломалось — бот сам пришлёт ⚠️ с причиной."
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
FIELD_ALIASES = {"source": "UTM_SOURCE", "src": "UTM_SOURCE", "medium": "UTM_MEDIUM",
                 "campaign": "UTM_CAMPAIGN", "camp": "UTM_CAMPAIGN", "content": "UTM_CONTENT",
                 "term": "UTM_TERM"}
SLASH_FIELD_COMMANDS = {"utmsource": "UTM_SOURCE", "utmmedium": "UTM_MEDIUM",
                        "utmcampaign": "UTM_CAMPAIGN", "utmcontent": "UTM_CONTENT",
                        "utmterm": "UTM_TERM"}
FIELD_LABELS = {"UTM_SOURCE": "источник (utm_source)", "UTM_MEDIUM": "канал (utm_medium)",
                "UTM_CAMPAIGN": "кампания (utm_campaign)", "UTM_CONTENT": "объявление (utm_content)",
                "UTM_TERM": "ключ (utm_term)"}


def build_menu(cfg: dict, field: str, date_from: str, date_to: str, title: str,
               clean: bool = False):
    """Меню со значениями UTM-поля за период (третий шаг /report и «utm»-команд)."""
    leads = fetch_leads(cfg, date_from, date_to)
    counts = [(value, count) for value, count in count_by(leads, field).items()
              if not (clean and value == "(без метки)")]
    if not counts:
        return f"Лидов за период {title} не было — выбирать не из чего."
    options = {str(i): {"btn": f"{value if len(value) <= 44 else value[:44] + '…'} · {count}",
                        "v": value}
               for i, (value, count) in enumerate(counts[:20])}
    return {"stage": "value", "text": f"🔎 {title} — выберите {FIELD_LABELS.get(field, field)}:",
            "field": field, "from": date_from, "to": date_to, "title": title,
            "options": options}


def period_menu() -> dict:
    """Первый шаг /report: выбор периода кнопками."""
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    periods = completed_periods(now)
    this_monday = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    items = [
        ("вчера", periods["day"]),
        ("сегодня", {"start": today, "end": today + timedelta(days=1),
                     "title": f"за {today:%d.%m.%Y} (сегодня)"}),
        ("эта неделя (с понедельника)",
         {"start": this_monday, "end": today + timedelta(days=1),
          "title": f"за неделю {this_monday:%d.%m}–{today:%d.%m.%Y}"}),
        ("этот месяц (с 1-го числа)",
         {"start": month_start, "end": today + timedelta(days=1),
          "title": f"за {MONTHS_RU[month_start.month - 1]} {month_start:%Y} (по сегодня)"}),
        ("прошлая неделя (Пн–Вс)", periods["week"]),
        ("прошлый месяц", periods["month"]),
    ]
    options = {str(i): {"btn": label, "from": p["start"].strftime(DATE_FORMAT),
                        "to": p["end"].strftime(DATE_FORMAT), "title": p["title"]}
               for i, (label, p) in enumerate(items)}
    return {"stage": "period", "text": "📅 Выберите период:", "options": options}


def field_menu(date_from: str, date_to: str, title: str) -> dict:
    """Второй шаг /report: весь отчёт или разбивка по одному UTM-полю."""
    items = [("📊 Весь отчёт", "all")] + \
            [(f"🔎 {label}", key) for key, label in FIELD_LABELS.items()]
    options = {str(i): {"btn": btn, "v": value} for i, (btn, value) in enumerate(items)}
    return {"stage": "field", "text": f"Что показать {title}?",
            "from": date_from, "to": date_to, "title": title, "options": options}


def send_menu(cfg: dict, state: dict, spath: Path, menu: dict, chat_id: str) -> None:
    """Регистрирует меню в state (живёт между запусками) и отправляет кнопки."""
    menus = state.setdefault("menus", {})
    mid = str(state.get("menu_seq", 0) + 1)
    state["menu_seq"] = int(mid)
    menu["chat"] = chat_id
    menu["clean"] = clean_mode(cfg, chat_id)
    menus[mid] = menu
    while len(menus) > 12:  # держим только свежие меню
        menus.pop(next(iter(menus)))
    save_state(spath, state)
    keyboard = [[{"text": option["btn"], "callback_data": f"rpt:{mid}:{idx}"}]
                for idx, option in menu["options"].items()]
    send_telegram(cfg["telegram_token"], chat_id, menu["text"],
                  reply_markup={"inline_keyboard": keyboard})
    log(f"меню отправлено в {chat_id}: {menu['text']} ({len(keyboard)} кнопок)")


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


def process_command(cfg: dict, text: str, clean: bool = False):
    """Ответ на команду из чата. None — молча проигнорировать (не команда).

    Возврат: строка (текст ответа) или dict-меню; clean — «чистый» режим чата.
    """
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]
    periods = completed_periods(datetime.now())

    # суффикс «utm <поле>» — меню с кнопками по значениям поля
    utm_field = None
    lower = [a.lower() for a in args]
    if "utm" in lower:
        i = lower.index("utm")
        if i + 1 < len(lower) and lower[i + 1] in FIELD_ALIASES:
            utm_field = FIELD_ALIASES[lower[i + 1]]
            args = args[:i] + args[i + 2:]
        else:
            return ("⚠️ После utm укажите поле: source, medium, campaign, "
                    "content или term. Пример: /day 14.08 utm source")

    def answer(date_from, date_to, title):
        if utm_field:
            return build_menu(cfg, utm_field, date_from, date_to, title, clean)
        return build_report(cfg, date_from, date_to, title, clean=clean)

    if cmd in ("/start", "/help"):
        return HELP_TEXT

    if cmd == "/report":
        return period_menu()

    # /utmsource, /utmcampaign, ... — меню кнопок из списка команд Telegram
    slash_field = SLASH_FIELD_COMMANDS.get(cmd.lstrip("/"))
    if slash_field:
        arg = args[0].lower() if args else ""
        if arg in ("week", "неделя"):
            period = periods["week"]
        elif arg in ("month", "месяц"):
            period = periods["month"]
        elif args:
            day = parse_day(args[0], today)
            if day is None:
                return (f"⚠️ Не понял аргумент. Примеры: /{cmd.lstrip('/')} 14.08, "
                        f"/{cmd.lstrip('/')} week, /{cmd.lstrip('/')} month или без аргумента (за вчера)")
            period = {"start": day, "end": day + timedelta(days=1),
                      "title": f"за {day:%d.%m.%Y}"}
        else:
            period = periods["day"]
        return build_menu(cfg, slash_field, period["start"].strftime(DATE_FORMAT),
                          period["end"].strftime(DATE_FORMAT), period["title"], clean)

    if cmd == "/day":
        day = parse_day(args[0] if args else "вчера", today)
        if day is None:
            return "⚠️ Не понял дату. Примеры: /day 14.08 или /day 14.08.2026"
        return answer(day.strftime(DATE_FORMAT),
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
        return answer(monday.strftime(DATE_FORMAT),
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
        return answer(start.strftime(DATE_FORMAT), end.strftime(DATE_FORMAT),
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
        return answer(d1.strftime(DATE_FORMAT),
                      (d2 + timedelta(days=1)).strftime(DATE_FORMAT),
                      f"за период {d1:%d.%m}–{d2:%d.%m.%Y}")

    if cmd.startswith("/"):
        return f"Не знаю команду {cmd}\n\n{HELP_TEXT}"
    return None


def handle_callback(cfg: dict, state: dict, spath: Path, cb: dict) -> None:
    """Нажатие кнопки меню: шаг /report, поле UTM или готовый отчёт по значению."""
    token = cfg["telegram_token"]

    def answer_cb(text=None):
        try:
            payload = {"callback_query_id": cb["id"]}
            if text:
                payload["text"] = text
            http_post_json(f"{TG_API}/bot{token}/answerCallbackQuery", payload, 15)
        except Exception:
            pass  # не критично, если не удалось снять «часики» с кнопки

    answer_cb()
    data = (cb.get("data") or "").split(":")
    menu = state.get("menus", {}).get(data[1]) if len(data) == 3 and data[0] == "rpt" else None
    option = menu.get("options", {}).get(data[2]) if menu else None
    if option is None:
        answer_cb("Меню устарело — вызовите команду заново")
        return

    chat = menu.get("chat") or chat_ids(cfg)[0]
    clean = menu.get("clean", False)
    stage = menu.get("stage", "value")
    if stage == "period":
        send_menu(cfg, state, spath, field_menu(option["from"], option["to"], option["title"]), chat)
    elif stage == "field":
        if option["v"] == "all":
            report = build_report(cfg, menu["from"], menu["to"], menu["title"], clean=clean)
            send_telegram(token, chat, report)
            log(f"кнопка: весь отчёт {menu['title']}")
        else:
            result = build_menu(cfg, option["v"], menu["from"], menu["to"], menu["title"], clean)
            if isinstance(result, str):
                send_telegram(token, chat, result)
            else:
                send_menu(cfg, state, spath, result, chat)
    else:
        report = build_report(cfg, menu["from"], menu["to"], menu["title"],
                              extra={menu["field"]: option["v"]}, clean=clean)
        send_telegram(token, chat, report)
        log(f"кнопка: {menu['field']}={option['v']} → отчёт {menu['title']}")


def handle_commands(cfg: dict, poll_seconds: int) -> None:
    """Читает команды из Telegram (getUpdates) и отвечает отчётами.

    poll_seconds > 0 — слушать непрерывно столько секунд (long polling),
    0 — одна короткая проверка накопившихся команд.
    Смещение update_id хранится в state.json, поэтому команды не теряются
    и не обрабатываются дважды между запусками.
    """
    spath = state_path_for(cfg)
    state = load_state(spath)
    chats = set(chat_ids(cfg))
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
                                        "allowed_updates": ["message", "callback_query"]},
                                  timeout + 15)
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

            callback = update.get("callback_query")
            if callback:
                try:
                    handle_callback(cfg, state, spath, callback)
                except Exception as err:
                    log(f"ошибка обработки кнопки: {err}")
                continue

            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            chat = str(message.get("chat", {}).get("id", ""))
            if not text or not chat:
                continue
            if chat not in chats:
                # в неподключённых чатах подсказываем chat_id — так группу легко
                # добавить в конфиг; данные при этом не отдаём
                log(f"сообщение из неподключённого чата {chat}: {text[:40]}")
                if text.startswith("/"):
                    try:
                        send_telegram(cfg["telegram_token"], chat,
                                      "⛔️ Этот чат ещё не подключён к отчётам.\n"
                                      f"chat_id этого чата: <code>{chat}</code>\n"
                                      "Передайте его администратору бота.")
                    except Exception:
                        pass
                continue
            try:
                reply = process_command(cfg, text, clean=clean_mode(cfg, chat))
            except Exception as err:
                reply = f"⚠️ Не удалось собрать отчёт: {html.escape(str(err)[:400])}"
            if reply is None:
                continue
            try:
                if isinstance(reply, dict):  # меню с кнопками
                    send_menu(cfg, state, spath, reply, chat)
                else:
                    send_telegram(cfg["telegram_token"], chat, reply)
                    log(f"команда «{text}» выполнена (чат {chat})")
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
                key = {"yesterday": "day"}.get(args.period, args.period) or "day"
                period = completed_periods(now)[key]
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
