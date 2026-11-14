"""
Мониторинг Wallet P2P (USDT, TON/GRAM и др.) с управлением из Telegram.

Установка:  pip install httpx
Запуск:     python3 wallet_p2p_bot_multi.py     (скрипт должен оставаться запущенным)

Команды в чате с ботом (работают только из чата CHAT_ID):
  /start          - запустить мониторинг
  /stop           - поставить на паузу
  /limit 5000     - присылать объявления, где верхний лимит не ниже 5000
  /limit off      - отключить проверку лимита
  /price USDT 92  - изменить порог цены монеты (/price TON 105, /price без аргументов - список)
  /status         - текущее состояние
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path

import httpx


def load_env_file() -> None:
    """Читает файл .env рядом со скриптом (строки KEY=VALUE). Уже заданные переменные не трогает."""
    path = Path(__file__).with_name(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

# ================== НАСТРОЙКИ ==================
# Секреты берутся из переменных окружения или из файла .env (см. env.example).
# Прямо в коде их хранить не нужно.
WALLET_API_KEY = os.environ.get("WALLET_API_KEY", "PUT_WALLET_API_KEY")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))

# Что отслеживаем. У каждой строки свой порог цены (в фиате).
# "alts" - запасные коды монеты на случай ответа 400 (бот запомнит рабочий).
WATCHES = [
    {"crypto": "USDT", "fiat": "RUB", "side": "SELL", "max_price": 90.0,  "alts": []},
    {"crypto": "TON",  "fiat": "RUB", "side": "SELL", "max_price": 100.0, "alts": ["GRAM"]},
]

PAGE_SIZE = 50
POLL_INTERVAL = 10                 # сек между кругами (данные в API обновляются ~раз в 30 с)
RENOTIFY_AFTER = 300               # не повторять то же объявление чаще N сек

ONLY_ONLINE = True
MIN_EXEC_RATE = 0.0                # мин. рейтинг мейкера, 0 = без фильтра
DEFAULT_LIMIT = 3000               # начальный верхний лимит; дальше меняется командой /limit
SHOW_RAW_ITEM = False              # True - писать в консоль полные данные объявлений
# ===============================================

URL = "https://p2p.walletbot.me/p2p/integration-api/v1/item/online"
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
# DATA_DIR - папка, где хранится settings.json (на хостинге укажи постоянный диск/volume)
SETTINGS_FILE = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent))) / "settings.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в логах виден токен бота
log = logging.getLogger("wallet-p2p")

state = {"running": True, "limit": DEFAULT_LIMIT}   # limit=None - проверка отключена
last_sent: dict[str, float] = {}
last_min: dict[str, float] = {}
price_overrides: dict[str, float] = {}   # пороги, изменённые командой /price
resolved: dict[str, str] = {}


# ---------- сохранение настроек ----------
def watch_key(w: dict) -> str:
    return f'{w["crypto"]}/{w["fiat"]}'


def load_settings() -> None:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if "limit" in data:
            state["limit"] = data["limit"]
        for key, value in (data.get("prices") or {}).items():
            for w in WATCHES:
                if watch_key(w) == key:
                    w["max_price"] = float(value)
                    price_overrides[key] = float(value)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Не удалось прочитать settings.json: %s", e)


def save_settings() -> None:
    try:
        SETTINGS_FILE.write_text(
            json.dumps({"limit": state["limit"], "prices": price_overrides}), encoding="utf-8")
    except Exception as e:
        log.warning("Не удалось сохранить settings.json: %s", e)


def find_watches(name: str, fiat: str | None = None) -> list[dict]:
    name = name.upper()
    found = []
    for w in WATCHES:
        names = {w["crypto"].upper(), *[a.upper() for a in w.get("alts", [])],
                 resolved.get(w["crypto"], "").upper()}
        if name in names and (fiat is None or fiat.upper() == w["fiat"].upper()):
            found.append(w)
    return found


def fmt_limit() -> str:
    return "отключена" if state["limit"] is None else f'{state["limit"]:g}'


# ---------- Telegram ----------
async def notify(http: httpx.AsyncClient, text: str) -> None:
    try:
        r = await http.post(
            f"{TG}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        if r.status_code != 200:
            log.error("Telegram отклонил сообщение: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("Не удалось отправить уведомление: %s", e)


HELP = (
    "Команды:\n"
    "/start - запустить мониторинг\n"
    "/stop - пауза\n"
    "/limit 5000 - верхний лимит объявления не ниже 5000\n"
    "/limit off - отключить проверку лимита\n"
    "/price USDT 92 - порог цены для монеты (еще: /price TON 105)\n"
    "/price - показать текущие пороги\n"
    "/status - состояние"
)


def status_text() -> str:
    lines = [
        f'Мониторинг: {"работает ▶️" if state["running"] else "на паузе ⏸"}',
        f"Верхний лимит: {fmt_limit()}",
        f"Интервал опроса: {POLL_INTERVAL} с",
    ]
    for w in WATCHES:
        code = resolved.get(w["crypto"], w["crypto"])
        cur = last_min.get(f'{code}/{w["fiat"]}')
        lines.append(f'{code}/{w["fiat"]}: порог {w["max_price"]}'
                     + (f", сейчас мин. цена {cur}" if cur is not None else ""))
    return "\n".join(lines)


async def handle_command(http: httpx.AsyncClient, text: str) -> None:
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]

    if cmd in ("/start", "/run"):
        state["running"] = True
        await notify(http, "▶️ Мониторинг запущен\n\n" + status_text())
    elif cmd == "/stop":
        state["running"] = False
        await notify(http, "⏸ Мониторинг остановлен. Чтобы продолжить: /start")
    elif cmd == "/status":
        await notify(http, status_text())
    elif cmd == "/limit":
        if len(parts) < 2:
            await notify(http, f"Верхний лимит сейчас: {fmt_limit()}\nИзменить: /limit 5000 или /limit off")
            return
        arg = parts[1].lower()
        if arg in ("off", "нет", "none"):
            state["limit"] = None
        else:
            try:
                value = float(arg.replace(",", "."))
                if value <= 0:
                    raise ValueError
            except ValueError:
                await notify(http, "Не понял число. Пример: /limit 5000")
                return
            state["limit"] = value
        save_settings()
        await notify(http, f"✅ Верхний лимит: {fmt_limit()}")
    elif cmd == "/price":
        if len(parts) == 1:
            lines = ["Пороги цен:"] + [f'{watch_key(w)}: {w["max_price"]:g}' for w in WATCHES]
            lines.append("Изменить: /price USDT 92")
            await notify(http, "\n".join(lines))
            return
        if len(parts) not in (3, 4):
            await notify(http, "Пример: /price USDT 92 или /price USDT RUB 92")
            return
        fiat = parts[2] if len(parts) == 4 else None
        try:
            value = float(parts[-1].replace(",", "."))
            if value <= 0:
                raise ValueError
        except ValueError:
            await notify(http, "Не понял число. Пример: /price USDT 92")
            return
        targets = find_watches(parts[1], fiat)
        if not targets:
            have = ", ".join(watch_key(w) for w in WATCHES)
            await notify(http, f"Не нашёл такую монету. Сейчас отслеживаются: {have}")
            return
        for w in targets:
            w["max_price"] = value
            price_overrides[watch_key(w)] = value
        save_settings()
        await notify(http, "✅ Порог цены: " + ", ".join(f"{watch_key(w)} → {value:g}" for w in targets))
    else:
        await notify(http, HELP)


async def command_loop(http: httpx.AsyncClient) -> None:
    offset = None
    try:  # пропускаем команды, накопившиеся, пока бот был выключен
        r = await http.get(f"{TG}/getUpdates", params={"offset": -1, "timeout": 0}, timeout=10)
        res = r.json().get("result", [])
        if res:
            offset = res[-1]["update_id"] + 1
    except Exception as e:
        log.warning("getUpdates (старт): %s", e)

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            r = await http.get(f"{TG}/getUpdates", params=params, timeout=40)
            if r.status_code != 200:
                log.warning("getUpdates: %s %s", r.status_code, r.text[:200])
                await asyncio.sleep(5)
                continue
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                if msg.get("chat", {}).get("id") != CHAT_ID:
                    continue  # чужие сообщения игнорируем
                if msg.get("text"):
                    await handle_command(http, msg["text"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Ошибка приёма команд: %s", e)
            await asyncio.sleep(5)


# ---------- фильтры и ссылка ----------
def reject_reason(item: dict, max_price: float):
    """None, если объявление подходит; иначе причина отказа."""
    try:
        if float(item["price"]) >= max_price:
            return "цена не ниже порога"
        if ONLY_ONLINE and not item.get("isOnline", True):
            return "продавец не в сети"
        if float(item.get("executeRate") or 0) < MIN_EXEC_RATE:
            return f'рейтинг {item.get("executeRate")} ниже {MIN_EXEC_RATE}'
        if state["limit"] is not None:
            hi = float(item["maxAmount"])
            if hi < state["limit"]:
                return f'верхний лимит {hi:g} ниже {state["limit"]:g}'
        return None
    except (KeyError, TypeError, ValueError) as e:
        return f"не удалось разобрать объявление ({e})"


def suitable(item: dict, max_price: float) -> bool:
    return reject_reason(item, max_price) is None


def offer_link(it: dict) -> str:
    """Ссылка на объявление в Wallet.
    Формат (гипотеза, проверь на практике): offerid_<id>_<userId продавца>."""
    for k in ("link", "url", "deepLink", "shareLink"):
        if it.get(k):
            return str(it[k])
    suffix = f"_{it['userId']}" if it.get("userId") else ""
    raw = f"operation=offerid_{it['id']}{suffix}"
    b64 = base64.b64encode(raw.encode()).decode().rstrip("=")
    return f"https://t.me/walt/start?startapp=v2-{b64}"


# ---------- опрос Wallet ----------
async def request_items(http: httpx.AsyncClient, w: dict):
    """Возвращает (код_монеты, список объявлений) или None при 429."""
    first = resolved.get(w["crypto"], w["crypto"])
    candidates = [first] + [c for c in [w["crypto"], *w.get("alts", [])] if c != first]
    for code in candidates:
        r = await http.post(
            URL,
            headers={"X-API-Key": WALLET_API_KEY, "accept": "application/json"},
            json={"cryptoCurrency": code, "fiatCurrency": w["fiat"],
                  "side": w["side"], "page": 1, "pageSize": PAGE_SIZE},
            timeout=10,
        )
        if r.status_code == 401:
            raise SystemExit("401: неверный API-ключ")
        if r.status_code == 429:
            log.warning("429: слишком частые запросы, пауза 60 с")
            await asyncio.sleep(60)
            return None
        if r.status_code == 400:
            log.warning("400 для кода монеты %s - пробую следующий вариант", code)
            continue
        r.raise_for_status()
        resolved[w["crypto"]] = code
        return code, r.json().get("data", [])
    raise RuntimeError(f'API отклонил все коды для {w["crypto"]} (400). Проверь название монеты.')


async def check_watch(http: httpx.AsyncClient, w: dict) -> None:
    res = await request_items(http, w)
    if res is None:
        return
    code, items = res
    prices = [float(i["price"]) for i in items if "price" in i]
    if prices:
        last_min[f'{code}/{w["fiat"]}'] = min(prices)
    log.info("%s/%s: объявлений %d, мин. цена %s (порог %s)",
             code, w["fiat"], len(items), min(prices, default="-"), w["max_price"])

    for it in sorted(items, key=lambda i: float(i.get("price", 1e18)))[:5]:
        if float(it.get("price", 1e18)) < w["max_price"]:
            why = reject_reason(it, w["max_price"])
            log.info("   объявление %s по %s: %s", it.get("nickname", "?"), it.get("price"),
                     "ПОДХОДИТ" if why is None else f"отброшено - {why}")

    now = time.time()
    for it in items:
        if not suitable(it, w["max_price"]):
            continue
        key = f'{code}:{w["fiat"]}:{it["id"]}:{it["price"]}'
        if now - last_sent.get(key, 0) < RENOTIFY_AFTER:
            continue
        last_sent[key] = now
        if SHOW_RAW_ITEM:
            log.info("RAW: %s", json.dumps(it, ensure_ascii=False))
        await notify(
            http,
            f'🔔 Wallet P2P {code}/{w["fiat"]}\n'
            f'Цена: {it["price"]} (порог {w["max_price"]})\n'
            f'Продавец: {it.get("nickname", "?")} '
            f'(рейтинг {it.get("executeRate", "?")}, {"в сети" if it.get("isOnline") else "не в сети"})\n'
            f'Лимиты: {it.get("minAmount", "?")} - {it.get("maxAmount", "?")} {w["fiat"]}\n'
            f'Оплата: {", ".join(it.get("payments") or []) or "?"}\n'
            f'Номер: {it.get("number", "?")}\n'
            f'Ссылка: {offer_link(it)}',
        )


async def monitor_loop(http: httpx.AsyncClient) -> None:
    while True:
        if not state["running"]:
            await asyncio.sleep(1)
            continue
        started = time.time()
        for w in WATCHES:
            if not state["running"]:
                break
            try:
                await check_watch(http, w)
            except SystemExit:
                raise
            except Exception as e:
                log.warning("%s: %s", w["crypto"], e)
            await asyncio.sleep(1)  # пауза между запросами
        await asyncio.sleep(max(0, POLL_INTERVAL - (time.time() - started)))


async def main() -> None:
    if WALLET_API_KEY.startswith("PUT_") or BOT_TOKEN.startswith("PUT_") or not CHAT_ID:
        raise SystemExit("Не заданы WALLET_API_KEY, BOT_TOKEN или CHAT_ID (файл .env или переменные окружения)")
    load_settings()
    async with httpx.AsyncClient() as http:
        try:
            await http.post(f"{TG}/setMyCommands", timeout=10, json={"commands": [
                {"command": "start", "description": "Запустить мониторинг"},
                {"command": "stop", "description": "Остановить (пауза)"},
                {"command": "limit", "description": "Верхний лимит: /limit 5000"},
                {"command": "price", "description": "Порог цены: /price USDT 92"},
                {"command": "status", "description": "Состояние бота"},
            ]})
        except Exception as e:
            log.warning("setMyCommands: %s", e)
        await notify(http, "✅ Бот запущен\n\n" + status_text() + "\n\n" + HELP)
        await asyncio.gather(monitor_loop(http), command_loop(http))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
