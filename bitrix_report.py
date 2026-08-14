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
        # чаты, чьи отчёты приходят без UTM-списков
        "no_utm_chats": split("BTR_NO_UTM_CHATS"),
        # административный чат — здесь задаётся план (/plan)
        "admin_chats": split("BTR_ADMIN_CHATS"),
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
    fields = ["ID", "STATUS_ID", "DATE_CREATE", "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN",
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


ATTR_CACHE = {}


def attribution_map(cfg: dict, date_to: str) -> dict:
    """Телефон -> {"source": метка последнего лида, "first": (дата, метка) первого}.
    История за 90 дней до date_to. Нужна для атрибуции: человек взял бесплатный
    урок по рекламе, а платный курс оформил позже с другого устройства."""
    key = (cfg["bitrix_webhook"], date_to[:10])
    if key in ATTR_CACHE:
        return ATTR_CACHE[key]
    end = datetime.strptime(date_to, DATE_FORMAT)
    start = (end - timedelta(days=90)).strftime(DATE_FORMAT)
    webhook = cfg["bitrix_webhook"]
    mapping, fetched, cursor = {}, 0, 0
    while True:
        data = call_bitrix(webhook, "crm.lead.list", {
            "filter": {">=DATE_CREATE": start, "<DATE_CREATE": date_to},
            "select": ["ID", "PHONE", "UTM_SOURCE", "DATE_CREATE"],
            "order": {"DATE_CREATE": "ASC"}, "start": cursor})
        page = data.get("result", [])
        for lead in page:
            source = (lead.get("UTM_SOURCE") or "").strip()
            phone = user_key(lead)
            if not source or phone.startswith("lead-"):
                continue
            rec = mapping.setdefault(phone, {"source": source, "first": None})
            rec["source"] = source  # сортировка ASC: последняя метка перезапишет
            if rec["first"] is None:
                try:
                    first_dt = datetime.fromisoformat(lead.get("DATE_CREATE", ""))
                except ValueError:
                    continue
                rec["first"] = (first_dt, source)
        fetched += len(page)
        total = int(data.get("total") or 0)
        if not page or fetched >= total or fetched >= 3000:
            break
        cursor += len(page)
    ATTR_CACHE[key] = mapping
    return mapping


def clean_attribution_sections(cfg: dict, leads: list, date_to: str) -> list:
    """Блоки для рабочей группы: бесплатные уроки и попытки оплатить —
    только по меткам. Если у лида метки нет, источник ищется по телефону
    в истории за 90 дней; у оплат показываем первый контакт и цикл продажи."""
    mapping = attribution_map(cfg, date_to)
    free_groups, users = {}, {}

    for lead in leads:
        key = user_key(lead)
        own = (lead.get("UTM_SOURCE") or "").strip()
        user = users.setdefault(key, {"source": own, "paid": set(), "pay_date": None})
        if own and not user["source"]:
            user["source"] = own
        free_set, paid_set = set(), set()
        raw = lead.get("UF_CRM_PRODUCT") or []
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            for part in str(item).split(";"):
                name, is_free = normalize_product(part)
                if name:
                    (free_set if is_free else paid_set).add(name)
        source = own or mapping.get(key, {}).get("source", "")
        if source and free_set:
            group = free_groups.setdefault(source, {})
            for name in free_set:
                group[name] = group.get(name, 0) + 1
        user["paid"].update(paid_set)
        try:
            created = datetime.fromisoformat(lead.get("DATE_CREATE") or "")
            if paid_set and (user["pay_date"] is None or created > user["pay_date"]):
                user["pay_date"] = created
        except ValueError:
            pass

    paid_groups, first_notes = {}, {}
    for key, user in users.items():
        if not user["paid"]:
            continue
        info = mapping.get(key) or {}
        source = user["source"] or info.get("source", "")
        if not source:
            continue
        group = paid_groups.setdefault(source, {})
        for name in user["paid"]:
            group[name] = group.get(name, 0) + 1
        first = info.get("first")
        if first and user["pay_date"]:
            first_dt, first_source = first
            days = max((user["pay_date"] - first_dt).days, 0)
            first_notes.setdefault(source, []).append(
                f"первый контакт: {first_dt:%d.%m} по {first_source} · {days} дн. до оплаты")

    lines = []

    def render_header(groups, title, emoji):
        if not groups:
            return False
        lines.append("")
        lines.append(f"<b>{emoji} {title}:</b>")
        return True

    if render_header(free_groups, "Бесплатные уроки по меткам", "🎁"):
        for source, names in sorted(free_groups.items(),
                                    key=lambda kv: -sum(kv[1].values()))[:5]:
            lines.append(f"• {fmt(source)} — {sum(names.values())}")
            for name, count in sorted(names.items(), key=lambda kv: -kv[1])[:5]:
                lines.append(f"↳ {fmt(name, 60)} — {plural_times(count)}")

    if render_header(paid_groups,
                     "Попытка оплатить по меткам (поиск по телефону за 90 дней)", "💳"):
        for source, names in sorted(paid_groups.items(),
                                    key=lambda kv: -sum(kv[1].values()))[:5]:
            lines.append(f"• {fmt(source)} — {sum(names.values())}")
            for name, count in sorted(names.items(), key=lambda kv: -kv[1])[:5]:
                lines.append(f"↳ {fmt(name, 60)} — {plural_times(count)}")
            for note in first_notes.get(source, [])[:3]:
                lines.append(f"↳ {note}")
    return lines


def weekly_lead_counts(cfg: dict, date_to: str, weeks: int = 8,
                       attributed: bool = False) -> dict:
    """Счёт СОЗДАННЫХ лидов по календарным неделям (Пн–Вс) за N недель.
    attributed=True — считать только лиды с меткой (своей или найденной по
    телефону в истории за 90 дней) — для рекламного чата."""
    end = datetime.strptime(date_to, DATE_FORMAT)
    start = (end - timedelta(days=7 * weeks)).strftime(DATE_FORMAT)
    webhook = cfg["bitrix_webhook"]
    flt = {">=DATE_CREATE": start, "<DATE_CREATE": date_to}
    select = ["ID", "DATE_CREATE"]
    mapping = attribution_map(cfg, date_to) if attributed else None
    if mapping is not None:
        select += ["PHONE", "UTM_SOURCE"]
    counts, cursor, fetched = {}, 0, 0
    while True:
        data = call_bitrix(webhook, "crm.lead.list",
                           {"filter": flt, "select": select, "start": cursor})
        page = data.get("result", [])
        for lead in page:
            if mapping is not None:
                own = (lead.get("UTM_SOURCE") or "").strip()
                if not own and user_key(lead) not in mapping:
                    continue  # без метки и телефон в истории не найден
            try:
                created = datetime.fromisoformat(lead.get("DATE_CREATE") or "")
            except ValueError:
                continue
            monday = (created.replace(hour=0, minute=0, second=0, microsecond=0)
                      - timedelta(days=created.weekday()))
            key = monday.strftime("%Y-%m-%d")
            counts[key] = counts.get(key, 0) + 1
        fetched += len(page)
        total = int(data.get("total") or 0)
        if not page or fetched >= total or fetched >= 5000:
            return counts
        cursor += len(page)


def forecast_next(series: list) -> int:
    """Прогноз следующего значения: линейный тренд, при малом числе точек — среднее."""
    ys = series[-6:]
    if not ys:
        return 0
    if len(ys) < 3:
        return max(round(sum(ys) / len(ys)), 1)
    n = len(ys)
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    intercept = my - slope * mx
    return max(round(intercept + slope * n), 1)


def current_week_projection(cfg: dict):
    """Проекция текущей недели: факт с понедельника + средние по оставшимся
    дням недели (с учётом сезона дня недели). -> (факт, итог, осталось дней)."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - timedelta(days=today.weekday())
    counts = daily_lead_counts(cfg, (today + timedelta(days=1)).strftime(DATE_FORMAT))
    hist = sorted(counts.items())
    mkey = monday.strftime("%Y-%m-%d")
    wtd = sum(c for d, c in hist if d >= mkey)
    last14 = [c for _, c in hist[-14:]]
    overall = sum(last14) / len(last14) if last14 else 0
    future = 0
    day = today + timedelta(days=1)
    while day.weekday() != 0:  # завтра .. воскресенье
        same = [c for k, c in hist
                if datetime.strptime(k, "%Y-%m-%d").weekday() == day.weekday()]
        avg = sum(same) / len(same) if same else overall
        future += round(avg)
        day += timedelta(days=1)
    return wtd, wtd + future, 7 - today.weekday() - 1


def build_forecast_message(cfg: dict, state: dict, week_start: datetime,
                           week_end: datetime, clean: bool = False) -> str:
    """Недельное сообщение: Цель → Реальность → Прогноз. В чистом (рекламном)
    чате динамика — только по лидам с метками (безымянных ищем по телефону),
    в обычном — по всем созданным лидам."""
    counts = weekly_lead_counts(cfg, week_end.strftime(DATE_FORMAT))
    ordered = sorted(counts.items())
    key = week_start.strftime("%Y-%m-%d")
    actual = counts.get(key, 0)
    prev = counts.get((week_start - timedelta(days=7)).strftime("%Y-%m-%d"), 0)
    history = [c for _, c in ordered]

    if prev > 0:
        change = (actual - prev) / prev * 100
    else:
        change = 100.0 if actual else 0.0
    if change <= -5:
        verdict = f"⚠️ Упали на {abs(change):.0f}% — проверьте бюджет и каналы, само не восстановится"
    elif change < 5:
        verdict = "😐 Роста нет — старыми действиями план не закрыть"
    elif change < 25:
        verdict = f"🙂 Растём (+{change:.0f}%) — усиливайте то, что работает"
    else:
        verdict = f"🚀 Рост +{change:.0f}% — резко усиливайте то, что работает"

    stored = (state.get("forecasts") or {}).get(key)
    if stored:
        diff = (actual - stored) / stored * 100 if stored else 0
        mark = "✅" if diff >= 0 else "❌"
        check = f"был <b>{stored}</b> → факт <b>{actual}</b> ({diff:+.0f}%) {mark}"
    else:
        check = "не было (первый выпуск — теперь веду)"

    next_pred = forecast_next(history)
    next_monday = week_start + timedelta(days=7)
    forecasts = state.setdefault("forecasts", {})
    forecasts[next_monday.strftime("%Y-%m-%d")] = next_pred
    while len(forecasts) > 4:
        forecasts.pop(next(iter(forecasts)))

    # цель и факты по ней
    plan = state.get("plan") or {}
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_goal = int(plan.get("daily") or 0) \
        if plan.get("month") == today.strftime("%Y-%m") else 0
    goal_line = f"🎯 <b>Цель: {daily_goal} лидов в день</b>\n\n" if daily_goal else ""
    yesterday = today - timedelta(days=1)
    y_count = count_leads_between(cfg, yesterday.strftime(DATE_FORMAT),
                                  today.strftime(DATE_FORMAT))
    y_line = f"• Вчера ({yesterday:%d.%m}): <b>{y_count}</b>"
    if daily_goal:
        y_line += f" из {daily_goal}"
    month_lines = []
    if daily_goal:
        month_start = today.replace(day=1)
        elapsed = max((today - month_start).days + 1, 1)
        current = count_leads_between(cfg, month_start.strftime(DATE_FORMAT),
                                      (today + timedelta(days=1)).strftime(DATE_FORMAT))
        month_lines.append(f"• {MONTHS_RU[month_start.month - 1]}: {current} из "
                           f"{daily_goal * elapsed} ({current * 100 // (daily_goal * elapsed)}%) "
                           f"— идёт {current / elapsed:.0f}/день")

    trend = " → ".join(str(c) for c in history[-6:])
    if clean:
        attr_counts = weekly_lead_counts(cfg, week_end.strftime(DATE_FORMAT),
                                         attributed=True)
        trend = " → ".join(str(c) for _, c in sorted(attr_counts.items())[-6:])
        dyn_label = "Динамика по неделям (только с метками, безымянных ищем по телефону)"
    else:
        dyn_label = "Динамика по неделям (все созданные лиды)"
    try:
        wtd, projected, days_left_w = current_week_projection(cfg)
        current_week = (f"🔮 Текущая неделя: уже {wtd}, к воскресенью будет "
                        f"<b>~{projected} за неделю</b> (~{projected / 7:.0f}/день)\n")
    except Exception:
        current_week = ""
    forecast_gap = ""
    if daily_goal:
        short = daily_goal * 7 - next_pred
        if short > 0:
            forecast_gap = f" — до цели {daily_goal}/день не хватает {short} за неделю"
    return (f"{goal_line}"
            f"📉 <b>Реальность:</b>\n"
            f"{y_line}\n"
            f"• Неделя {week_start:%d.%m}–{week_end - timedelta(days=1):%d.%m.%Y}: "
            f"<b>{actual}</b> лидов (~{actual / 7:.0f}/день)\n"
            f"• {dyn_label}: {fmt(trend)}\n"
            f"• Мой прогноз на неделю: {check}\n"
            + ("\n".join(month_lines) + "\n" if month_lines else "") +
            f"\n{verdict}\n"
            f"{current_week}"
            f"🔮 Следующая неделя по текущему темпу: <b>~{next_pred} за неделю</b> "
            f"(~{next_pred / 7:.0f}/день){forecast_gap}")


def build_plan(cfg: dict, week_start: datetime, week_end: datetime, plan: dict = None) -> str:
    """«Реальный план»: если задан месячный план — считаем от остатка до цели
    (сколько нужно в неделю, чтобы догнать, с честной оценкой разрыва);
    без плана — цель +5% к тренду."""
    counts = weekly_lead_counts(cfg, week_end.strftime(DATE_FORMAT))
    key = week_start.strftime("%Y-%m-%d")
    actual = counts.get(key, 0)
    history = [c for _, c in sorted(counts.items())]
    trend = forecast_next(history)
    next_monday = week_start + timedelta(days=7)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    lines = ["🧭 <b>Задачи и фокус:</b>"]
    if plan and plan.get("month") == today.strftime("%Y-%m") and int(plan.get("daily") or 0):
        daily = int(plan["daily"])
        need_weekly = daily * 7
        gap = need_weekly / max(actual, 1)
        if gap >= 2:
            raz = "раза" if 2 <= gap < 5 else "раз"
            lines.append(f"• Поднять трафик в <b>{gap:.0f} {raz}</b> — сейчас "
                         f"~{actual / 7:.0f}/день при цели {daily}. Добавить 2–3 новых "
                         f"канала и усилить бюджет на лучших кампаниях. Не получится — "
                         f"снизить /plan до реального")
        elif gap >= 1.3:
            lines.append(f"• Ускориться в {gap:.1f} раза — с ~{actual / 7:.0f}/день до "
                         f"{daily}: усилить текущие кампании, этого хватит")
        else:
            lines.append(f"• Держать темп: ~{actual / 7:.0f}/день при цели {daily} — "
                         f"план выполняется ✅")
    else:
        target = max(round(max(trend, actual) * 1.05), 1)
        lines.append(f"• Вырасти на 5%: ≥ {target} лидов за неделю "
                     f"(~{-(-target // 7)}/день)")
        lines.append("• Задать постоянную цель: /plan 100 в группе руководителя")

    leads = fetch_leads(cfg, week_start.strftime(DATE_FORMAT),
                        week_end.strftime(DATE_FORMAT))
    if leads:
        no_utm = sum(1 for l in leads if not (l.get("UTM_SOURCE") or "").strip())
        if no_utm / len(leads) >= 0.3:
            lines.append(f"• Починить метки в формах: {no_utm * 100 // len(leads)}% лидов "
                         f"непонятно откуда — реклама вслепую. Макросы {{{{…}}}} не "
                         f"подставляются, отдайте задачу тому, кто делал формы")
        src_counts = count_by(leads, "UTM_SOURCE")
        if src_counts:
            top, top_n = next(iter(src_counts.items()))
            if top != "(без метки)" and top_n / len(leads) >= 0.5:
                lines.append(f"• Не складывать всё в один канал: «{fmt(top)}» даёт "
                             f"{top_n * 100 // len(leads)}% лидов — пустить 20% бюджета "
                             f"на тест нового источника")
    return "\n".join(lines)


def fmt(value: str, limit: int = 60) -> str:
    if len(value) > limit:
        value = value[:limit] + "…"
    return html.escape(value)


def build_report(cfg: dict, date_from: str, date_to: str, title: str,
                 extra: dict = None, clean: bool = False, utm_fields=None,
                 plan: dict = None) -> str:
    """Отчёт за период. clean=True — «чистый» режим без «(без метки)»;
    utm_fields: None — все 5 UTM-разделов, [] — без них, список — только эти;
    plan — месячный план (строка прогресса в шапке)."""
    utm_sources = cfg.get("utm_sources") or []
    leads = fetch_leads(cfg, date_from, date_to, extra)

    lines = [f"📊 <b>Отчёт по лидам {title}</b>"]
    if utm_sources:
        lines.append(f"источники: {fmt(', '.join(utm_sources))}")
    if extra:
        field, value = next(iter(extra.items()))
        lines.append(f"фильтр: {FIELD_LABELS.get(field, field)} = {fmt(value)}")
    if plan:
        progress = plan_progress_line(cfg, plan)
        if progress:
            lines.append(progress)
    lines.append("")

    if not leads:
        lines.append("Лидов по заданным статусам и источникам не было.")
        return "\n".join(lines)

    all_sections = (("По источникам (utm_source)", "UTM_SOURCE"),
                    ("По каналам (utm_medium)", "UTM_MEDIUM"),
                    ("По кампаниям (utm_campaign)", "UTM_CAMPAIGN"),
                    ("По объявлениям (utm_content)", "UTM_CONTENT"),
                    ("По ключам (utm_term)", "UTM_TERM"))
    sections = all_sections if utm_fields is None else \
        [s for s in all_sections if s[1] in utm_fields]
    for section_title, field in sections:
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

    if clean:
        # рабочая группа: бесплатные уроки и оплаты — только по меткам,
        # без метки источник ищется по телефону за 90 дней
        lines.extend(clean_attribution_sections(cfg, leads, date_to))
    else:
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


def no_utm_mode(cfg: dict, chat_id) -> bool:
    """Чат, чьи отчёты приходят без UTM-списков (только итоги и уроки)."""
    raw = cfg.get("no_utm_chats") or []
    if isinstance(raw, str):
        raw = [raw]
    return str(chat_id) in {str(c).strip() for c in raw}


def admin_mode(cfg: dict, chat_id) -> bool:
    """Административный чат: только здесь можно задавать план (/plan)."""
    raw = cfg.get("admin_chats") or []
    if isinstance(raw, str):
        raw = [raw]
    return str(chat_id) in {str(c).strip() for c in raw}


def count_leads_between(cfg: dict, date_from: str, date_to: str) -> int:
    """Подсчёт СОЗДАННЫХ лидов за период (без фильтра статусов): приток трафика
    не должен зависеть от того, в какой статус лид переехал позже."""
    webhook = cfg["bitrix_webhook"]
    flt = {">=DATE_CREATE": date_from, "<DATE_CREATE": date_to}
    data = call_bitrix(webhook, "crm.lead.list",
                       {"filter": flt, "select": ["ID"], "start": 0})
    total = data.get("total")
    return int(total) if total is not None else len(data.get("result", []))


def plan_progress_line(cfg: dict, plan: dict):
    """Строка прогресса дневного плана: сравнение со ВЧЕРА, не с сегодняшним."""
    if not plan or plan.get("month") != datetime.now().strftime("%Y-%m"):
        return None
    daily = int(plan.get("daily") or 0)
    if daily <= 0:
        return None
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today.replace(day=1)
    elapsed = max((today - month_start).days + 1, 1)
    expected = daily * elapsed
    current = count_leads_between(cfg, month_start.strftime(DATE_FORMAT),
                                  (today + timedelta(days=1)).strftime(DATE_FORMAT))
    yesterday = today - timedelta(days=1)
    y_count = count_leads_between(cfg, yesterday.strftime(DATE_FORMAT),
                                  today.strftime(DATE_FORMAT))
    ratio = current / expected if expected else 1
    if ratio >= 1.05:
        pace = "опережаем 🚀"
    elif ratio >= 0.95:
        pace = "в графике ✅"
    else:
        pace = f"отстаём ❌"
    return (f"🎯 План {daily}/день · вчера {y_count} · среднее {current / elapsed:.0f}/день · "
            f"{MONTHS_RU[month_start.month - 1]} {current * 100 // expected}% · {pace}")


def handle_plan_command(cfg: dict, state: dict, spath: Path, text: str) -> str:
    """/plan — план по лидам В ДЕНЬ (задаётся в чате руководителя)."""
    parts = text.split()
    daily = None
    if len(parts) > 1:
        arg = parts[1].lower()
        if arg in ("удалить", "off", "сброс"):
            if state.pop("plan", None) is not None:
                save_state(spath, state)
                return "🗑 План удалён."
            return "Плана и не было."
        if arg.isdigit():
            daily = int(arg)
    if daily is None:
        plan = state.get("plan")
        if not plan or plan.get("month") != datetime.now().strftime("%Y-%m") \
                or not plan.get("daily"):
            return ("🎯 Задание плана: <code>/plan 100</code> — сколько лидов "
                    "нужно В ДЕНЬ.\nСейчас план не задан.")
        return (f"Текущий план:\n{plan_progress_line(cfg, plan)}\n\n"
                "Изменить: /plan 150 · удалить: /plan удалить")
    if not 1 <= daily <= 100000:
        return "⚠️ Дай число от 1 до 100 000."
    state["plan"] = {"month": datetime.now().strftime("%Y-%m"), "daily": daily}
    save_state(spath, state)
    month_name = MONTHS_RU[datetime.now().month - 1]
    return (f"🎯 План принят: <b>{daily} лидов/день</b> "
            f"(≈ {daily * 31} за {month_name}).\n\n"
            f"{plan_progress_line(cfg, state['plan'])}")


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
        # вариант отчёта зависит от режима чата: чистый/обычный × с UTM-списками/без
        variants = {}
        for chat in rchats:
            variants.setdefault((clean_mode(cfg, chat), no_utm_mode(cfg, chat)), []).append(chat)
        for (clean, no_utm), chats in variants.items():
            send_to_all(cfg, build_report(cfg, date_from, date_to, rep["title"],
                                          clean=clean,
                                          utm_fields=[] if no_utm else None,
                                          plan=state.get("plan")),
                        chats=chats)
        state.setdefault("sent", {})[rep["kind"]] = rep["key"]
        save_state(spath, state)
        log(f"отчёт {rep['title']} отправлен в {len(rchats)} чат. "
            f"({len(variants)} варианта)")

        if rep["kind"] == "week":
            # вторым сообщением к недельному отчёту — прогноз по лидам
            # (в рекламном чате динамика только по лидам с метками)
            try:
                plain = [c for c in rchats if not clean_mode(cfg, c)]
                neat = [c for c in rchats if clean_mode(cfg, c)]
                if plain:
                    send_to_all(cfg, build_forecast_message(cfg, state, rep["start"], rep["end"]),
                                chats=plain)
                if neat:
                    send_to_all(cfg, build_forecast_message(cfg, state, rep["start"], rep["end"],
                                                            clean=True),
                                chats=neat)
                save_state(spath, state)
                log("прогноз по лидам отправлен")
            except Exception as err:
                log(f"прогноз не построен: {err}")

        if rep["kind"] == "day":
            # сразу после утреннего отчёта — прогноз на сегодня
            try:
                forecast = build_daily_forecast_message(cfg, state)
                send_to_all(cfg, forecast, chats=rchats)
                save_state(spath, state)
                log("дневной прогноз отправлен")
            except Exception as err:
                log(f"дневной прогноз не построен: {err}")


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
    "/report → период (вчера, сегодня, эта/прошлая неделя, этот/прошлый месяц, "
    "90 дней) → формат: краткий без UTM-списков, полный, один конкретный список "
    "или 📈 прогноз с реальным планом\n"
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
    "🎯 <b>ПЛАН ПО ЛИДАМ</b> (задаётся в группе руководителя):\n"
    "/plan 100 — сколько лидов нужно В ДЕНЬ · /plan — прогресс · "
    "/plan удалить — сброс. Прогресс виден в шапке каждого отчёта.\n"
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
    d90_start = today - timedelta(days=89)
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
        ("последние 90 дней",
         {"start": d90_start, "end": today + timedelta(days=1),
          "title": f"за 90 дней ({d90_start:%d.%m}–{today:%d.%m.%Y})"}),
    ]
    options = {str(i): {"btn": label, "from": p["start"].strftime(DATE_FORMAT),
                        "to": p["end"].strftime(DATE_FORMAT), "title": p["title"]}
               for i, (label, p) in enumerate(items)}
    # прогноз доступен сразу, первым тапом — без выбора периода и формата
    options["f"] = {"btn": "📈 Прогноз и план", "v": "forecast"}
    return {"stage": "period", "text": "📅 Выберите период:", "options": options}


def field_menu(date_from: str, date_to: str, title: str) -> dict:
    """Второй шаг /report: формат отчёта — краткий, полный или один UTM-список."""
    items = [("📊 Краткий (без UTM-списков)", "short"),
             ("📋 Полный (все UTM-списки)", "all")] + \
            [(f"🔎 Только {label.split(' (')[0]}", key)
             for key, label in FIELD_LABELS.items()]
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


def process_command(cfg: dict, text: str, clean: bool = False, no_utm: bool = False,
                    plan: dict = None):
    """Ответ на команду из чата. None — молча проигнорировать (не команда).

    Возврат: строка или dict-меню; clean/no_utm/plan — режимы чата.
    """
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]
    periods = completed_periods(datetime.now())
    fields = [] if no_utm else None

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
        return build_report(cfg, date_from, date_to, title, clean=clean,
                            utm_fields=fields, plan=plan)

    if cmd in ("/start", "/help"):
        return HELP_TEXT

    if cmd == "/report":
        return period_menu()

    # /utmsource, /utmcampaign, ... — меню кнопок из списка команд Telegram
    slash_field = SLASH_FIELD_COMMANDS.get(cmd.lstrip("/"))
    if slash_field:
        arg = args[0].lower() if args else ""
        today90 = today - timedelta(days=89)
        if arg in ("week", "неделя"):
            period = periods["week"]
        elif arg in ("month", "месяц"):
            period = periods["month"]
        elif arg in ("90", "90дней", "90 дней"):
            period = {"start": today90, "end": today + timedelta(days=1),
                      "title": f"за 90 дней ({today90:%d.%m}–{today:%d.%m.%Y})"}
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


def daily_lead_counts(cfg: dict, date_to: str, days: int = 28) -> dict:
    """Счёт СОЗДАННЫХ лидов по дням за N дней (без фильтра статусов — см.
    weekly_lead_counts)."""
    end = datetime.strptime(date_to, DATE_FORMAT)
    start = (end - timedelta(days=days)).strftime(DATE_FORMAT)
    webhook = cfg["bitrix_webhook"]
    flt = {">=DATE_CREATE": start, "<DATE_CREATE": date_to}
    counts, cursor, fetched = {}, 0, 0
    while True:
        data = call_bitrix(webhook, "crm.lead.list",
                           {"filter": flt, "select": ["ID", "DATE_CREATE"], "start": cursor})
        page = data.get("result", [])
        for lead in page:
            try:
                day = datetime.fromisoformat(lead.get("DATE_CREATE") or "")
            except ValueError:
                continue
            key = day.strftime("%Y-%m-%d")
            counts[key] = counts.get(key, 0) + 1
        fetched += len(page)
        total = int(data.get("total") or 0)
        if not page or fetched >= total or fetched >= 5000:
            return counts
        cursor += len(page)


WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг",
               "пятница", "суббота", "воскресенье"]


def build_daily_forecast_message(cfg: dict, state: dict) -> str:
    """Утренний прогноз на сегодня: средний темп + сезон дня недели,
    самопроверка вчерашнего прогноза и сверка с дневным планом."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    counts = daily_lead_counts(cfg, today.strftime(DATE_FORMAT))
    ordered = sorted(counts.items())
    last14 = [c for _, c in ordered[-14:]]
    avg14 = sum(last14) / len(last14) if last14 else 0
    same_dow = [c for day, c in ordered[-28:]
                if datetime.strptime(day, "%Y-%m-%d").weekday() == today.weekday()]
    dow_avg = sum(same_dow) / len(same_dow) if same_dow else avg14
    pred = max(round(0.5 * avg14 + 0.5 * dow_avg), 0)

    ykey = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    y_actual = counts.get(ykey, 0)
    forecasts = state.setdefault("daily_forecasts", {})
    stored = forecasts.get(ykey)
    if stored:
        diff = (y_actual - stored) / stored * 100
        check = f"Вчера: {y_actual} (прогноз был {stored} → {diff:+.0f}% " \
                f"{'✅' if diff >= -10 else '❌'})"
    else:
        check = f"Вчера: {y_actual} (прогноза не было — теперь веду)"

    forecasts[today.strftime("%Y-%m-%d")] = pred
    while len(forecasts) > 7:
        forecasts.pop(next(iter(forecasts)))

    plan = state.get("plan") or {}
    daily_goal = int(plan.get("daily") or 0) if \
        plan.get("month") == today.strftime("%Y-%m") else 0
    if daily_goal:
        if pred >= daily_goal:
            plan_line = f"🎯 Цель {daily_goal}/день — прогноз её закрывает ✅"
        else:
            mult = daily_goal / max(pred, 1)
            plan_line = (f"🎯 До цели не хватает {daily_goal - pred} — поднять "
                         f"темп в {mult:.1f} раза ❌")
    else:
        plan_line = "🎯 Цель не задана: /plan 100 в группе руководителя"

    return (f"☀️ <b>Прогноз на сегодня, {WEEKDAYS_RU[today.weekday()]} "
            f"{today:%d.%m}</b>\n"
            f"{check}\n"
            f"Темп: ~{avg14:.0f}/день (2 недели), по {WEEKDAYS_RU[today.weekday()]}м ~{dow_avg:.0f}\n\n"
            f"Прогноз на сегодня: <b>~{pred} лидов</b>\n"
            f"{plan_line}")


def forecast_and_plan_message(cfg: dict, state: dict, clean: bool = False) -> str:
    """Прогноз + реальный план по последней завершённой неделе."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - timedelta(days=today.weekday())
    week_start = monday - timedelta(days=7)
    week_end = week_start + timedelta(days=7)
    return (build_forecast_message(cfg, state, week_start, week_end, clean=clean)
            + "\n\n" + build_plan(cfg, week_start, week_end, plan=state.get("plan")))


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
        if option.get("v") == "forecast":
            send_telegram(token, chat, forecast_and_plan_message(cfg, state, clean=clean))
            log("кнопка: прогноз и план (из главного меню)")
            return
        send_menu(cfg, state, spath, field_menu(option["from"], option["to"], option["title"]), chat)
    elif stage == "field":
        value = option["v"]
        if value == "forecast":
            send_telegram(token, chat, forecast_and_plan_message(cfg, state, clean=clean))
            log("кнопка: прогноз и план")
            return
        utm_fields = None if value == "all" else ([] if value == "short" else [value])
        report = build_report(cfg, menu["from"], menu["to"], menu["title"],
                              clean=clean, utm_fields=utm_fields)
        send_telegram(token, chat, report)
        log(f"кнопка: отчёт {menu['title']} ({option['btn']})")
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
            if text.split()[0].lower().split("@")[0] == "/plan":
                try:
                    reply = (handle_plan_command(cfg, state, spath, text)
                             if admin_mode(cfg, chat)
                             else "🔒 Задавать план можно только в группе руководителя.")
                except Exception as err:
                    reply = f"⚠️ {html.escape(str(err)[:300])}"
                try:
                    send_telegram(cfg["telegram_token"], chat, reply)
                    log(f"команда «/plan» выполнена (чат {chat})")
                except Exception as err:
                    log(f"не удалось ответить на «/plan»: {err}")
                continue
            try:
                reply = process_command(cfg, text, clean=clean_mode(cfg, chat),
                                        no_utm=no_utm_mode(cfg, chat),
                                        plan=state.get("plan"))
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
