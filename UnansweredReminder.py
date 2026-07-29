"""
UnansweredReminder — плагин для FunPay Cardinal v0.1.17.9

Отслеживает диалоги, в которых последнее сообщение от покупателя
остаётся без ответа продавца дольше заданного времени,
и отправляет напоминание в Telegram с inline-кнопками.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    HAS_TELEGRAM = True
except ImportError:
    InlineKeyboardMarkup = None
    InlineKeyboardButton = None
    HAS_TELEGRAM = False

try:
    from tg_bot.CBT import CBT
except ImportError:
    try:
        from plugins.tg_bot.CBT import CBT
    except ImportError:
        CBT = type("CBT", (), {"PLUGIN_SETTINGS": "47"})


NAME = "UnansweredReminder"
VERSION = "1.0.0"
DESCRIPTION = "⏰ Неотвеченные диалоги → напоминание в Telegram. /list — список (прописывать перед 1 использованием), /cleanup — очистка"
CREDITS = "@zap90a"
UUID = "0f664d34-ddd9-418e-988a-ac834b3e397c"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.unanswered_reminder")

CHECK_INTERVAL_SEC = 30
CALLBACK_PREFIX = "ur_"
STORAGE_DIR = os.path.join("storage", "unanswered_reminder")
CONFIG_PATH = os.path.join(STORAGE_DIR, "config.json")
STATE_PATH = os.path.join(STORAGE_DIR, "state.json")
TIMEOUT_OPTIONS = [
    ("30 сек", 30), ("1 мин", 60), ("3 мин", 180),
    ("5 мин", 300), ("10 мин", 600), ("15 мин", 900), ("30 мин", 1800),
]
REMIND_INTERVAL_OPTIONS = [
    ("1 мин", 60), ("3 мин", 180), ("5 мин", 300),
    ("10 мин", 600), ("15 мин", 900), ("30 мин", 1800), ("60 мин", 3600),
]

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "timeout": 300,
    "check_all_dialogs": True,
    "remind_again": True,
    "remind_again_interval": 600,
    "exclude_closed": True,
    "ignore_system": True,
    "cleanup_max_age_hours": 24,
    "cleanup_max_chats": 500,
    "cleanup_interval_minutes": 60,
    "admin_chat_id": None,
}

DEFAULT_STATE: dict[str, Any] = {
    "ignored_chats": [],
    "notified": {},
    "notification_count": {},
    "last_message_time": {},
    "last_sender": {},
    "usernames": {},
    "last_cleanup": None,
}

_file_lock = threading.Lock()
_stop_event = threading.Event()
_config: dict[str, Any] = {}
_state: dict[str, Any] = {}
_cardinal_ref: Any = None
_admin_chat_id: Optional[int] = None
_telegram_bot: Any = None


def _get_timeout_sec() -> int:
    return _config.get("timeout", _config.get("timeout_minutes", 5) * 60)


def _get_remind_interval_sec() -> int:
    return _config.get("remind_again_interval", _config.get("remind_again_minutes", 10) * 60)


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    minutes = seconds // 60
    sec_remain = seconds % 60
    if sec_remain == 0:
        return f"{minutes} мин"
    return f"{minutes} мин {sec_remain} сек"


def _ensure_storage_dir() -> None:
    try:
        os.makedirs(STORAGE_DIR, exist_ok=True)
    except OSError as exc:
        logger.error(
            f"[UnansweredReminder] Не удалось создать {STORAGE_DIR}: {exc}")


def _write_json(path: str, data: dict) -> None:
    _ensure_storage_dir()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except (IOError, OSError) as exc:
        logger.error(f"[UnansweredReminder] Ошибка записи {path}: {exc}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _load_json(path: str, default: dict) -> dict:
    if not os.path.exists(path):
        return dict(default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {**default, **json.load(f)}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"[UnansweredReminder] Ошибка чтения {path}: {exc}")
        return dict(default)


def _parse_time(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (ValueError, TypeError):
            pass
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _build_kb(rows: list[list[dict]]) -> Any:
    if InlineKeyboardMarkup is not None:
        buttons = [[InlineKeyboardButton(**btn)
                    for btn in row] for row in rows]
        return InlineKeyboardMarkup(buttons)
    return {"inline_keyboard": [[dict(**btn) for btn in row] for row in rows]}


def _build_notify_kb(chat_id: str) -> Any:
    return _build_kb([
        [
            {"text": "✅ Я ответил", "callback_data": f"{CALLBACK_PREFIX}answer_{chat_id}"},
            {"text": "🔇 Игнорировать",
                "callback_data": f"{CALLBACK_PREFIX}ignore_{chat_id}"},
        ],
        [
            {"text": "📋 Инфо", "callback_data": f"{CALLBACK_PREFIX}info_{chat_id}"},
            {"text": "⚙️ Настройки", "callback_data": f"{CALLBACK_PREFIX}settings"},
        ],
    ])


def _build_settings_kb() -> Any:
    en = _config.get("enabled", True)
    to = _get_timeout_sec()
    al = _config.get("check_all_dialogs", True)
    rm = _config.get("remind_again", True)
    ri = _get_remind_interval_sec()
    ex = _config.get("exclude_closed", True)
    ig = _config.get("ignore_system", True)
    m = "Все диалоги" if al else "Только заказы"
    return _build_kb([
        [{"text": f'🔄 {"Выкл" if en else "Вкл"}',
            "callback_data": f"{CALLBACK_PREFIX}toggle"}],
        [{"text": f"⏱ Таймаут: {_format_duration(to)}",
            "callback_data": f"{CALLBACK_PREFIX}timeout"}],
        [{"text": f"👁 Режим: {m}", "callback_data": f"{CALLBACK_PREFIX}mode"}],
        [{"text": f'🔁 Повтор: {"Вкл" if rm else "Выкл"}',
            "callback_data": f"{CALLBACK_PREFIX}remind"}],
        [{"text": f"🔁 Интервал: {_format_duration(ri)}",
            "callback_data": f"{CALLBACK_PREFIX}remindinterval"}],
        [{"text": f'❌ Искл. закрытые: {"Да" if ex else "Нет"}',
            "callback_data": f"{CALLBACK_PREFIX}exclude"}],
        [{"text": f'🚫 Игнор. системные: {"Да" if ig else "Нет"}',
            "callback_data": f"{CALLBACK_PREFIX}ignoresystem"}],
        [
            {"text": "🗑 Очистить", "callback_data": f"{CALLBACK_PREFIX}cleanup"},
            {"text": "📊 Статистика", "callback_data": f"{CALLBACK_PREFIX}stats"},
        ],
        [{"text": "⬅ Назад", "callback_data": f"{CALLBACK_PREFIX}close"}],
    ])


def _resolve_admin_chat_id() -> Optional[int]:
    cid = _config.get("admin_chat_id")
    if cid is not None:
        try:
            logger.info(
                f"[UnansweredReminder] admin_chat_id из конфига: {cid}")
            return int(cid)
        except (ValueError, TypeError):
            pass
    cardinal = _cardinal_ref
    if cardinal is None:
        logger.warning(
            "[UnansweredReminder] cardinal_ref is None, не можем определить admin_chat_id")
        return None
    for attr in ("admin_id", "owner_id", "admin_chat_id", "master_id"):
        val = _safe_attr(cardinal, attr)
        if val is not None:
            logger.info(
                f"[UnansweredReminder] admin_chat_id из cardinal.{attr}: {val}")
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    cfg = _safe_attr(cardinal, "config", {})
    if isinstance(cfg, dict):
        for key in ("admin_id", "owner_id", "admin_chat_id", "master_id"):
            val = cfg.get(key)
            if val is not None:
                logger.info(
                    f"[UnansweredReminder] admin_chat_id из cardinal.config['{key}']: {val}")
                try:
                    return int(val)
                except (ValueError, TypeError):
                    pass
    logger.info(
        "[UnansweredReminder] admin_chat_id не найден. Отправьте /list боту в Telegram для авто-захвата.")
    return None


def _get_dialogs() -> list:
    cardinal = _cardinal_ref
    if cardinal is None:
        return []
    api = _safe_attr(cardinal, "api")
    if api is not None:
        getter = _safe_attr(api, "get_dialogs") or _safe_attr(api, "get_chats")
        if getter is not None:
            try:
                return getter() or []
            except Exception as exc:
                logger.debug(f"[UnansweredReminder] API get_dialogs: {exc}")
    account = _safe_attr(cardinal, "account")
    if account is not None:
        getter = (_safe_attr(account, "get_dialogs") or
                  _safe_attr(account, "get_chats") or
                  _safe_attr(account, "get_conversations"))
        if getter is not None:
            try:
                return getter() or []
            except Exception as exc:
                logger.debug(
                    f"[UnansweredReminder] account get_dialogs: {exc}")
    return []


def _extract_chat_id(dialog: dict) -> Optional[str]:
    for key in ("id", "chat_id", "chatId", "dialog_id", "dialogId", "cid"):
        val = dialog.get(key)
        if val is not None:
            return str(val)
    return None


def _get_order_status(dialog: dict) -> Optional[str]:
    st = dialog.get("status") or dialog.get("order_status")
    if st:
        return str(st).lower()
    for key in ("order", "offer", "lot"):
        obj = dialog.get(key)
        if isinstance(obj, dict):
            s = obj.get("status") or obj.get("order_status")
            if s:
                return str(s).lower()
    return None


def _save_username(chat_id: str, username: str) -> None:
    _state.setdefault("usernames", {})[chat_id] = username
    _save_state()


def _fetch_dialog_username(chat_id: str) -> Optional[str]:
    cardinal = _cardinal_ref
    if cardinal is None:
        return None

    chat_id_int = int(chat_id) if chat_id.isdigit() else chat_id
    cid_values = [chat_id, chat_id_int, str(chat_id_int)]

    account = _safe_attr(cardinal, "account")
    if account is not None:
        for attr in ("chats", "dialogues", "dialogs", "conversations", "chat_data", "dialog_data", "chat_list"):
            storage = _safe_attr(account, attr)
            if storage is None:
                continue
            storage_type = type(storage).__name__
            storage_len = len(storage) if hasattr(storage, '__len__') else '?'
            logger.info(
                f"[UnansweredReminder] _fetch_dialog_username: "
                f"account.{attr} = {storage_type}(len={storage_len})")
            if isinstance(storage, dict):
                sample_keys = list(storage.keys())[:5]
                logger.info(
                    f"[UnansweredReminder]   account.{attr} keys: {sample_keys}")
                for cid in cid_values:
                    data = storage.get(cid)
                    if isinstance(data, dict):
                        logger.info(
                            f"[UnansweredReminder]   найдена запись для {cid}, поля: {list(data.keys())[:15]}")
                        for key in ("username", "user_name", "name", "nickname", "buyer_name", "seller_name", "login"):
                            val = data.get(key)
                            if val:
                                _save_username(chat_id, str(val))
                                return str(val)
                        for obj_key in ("user", "buyer", "customer", "interlocutor", "seller"):
                            obj = data.get(obj_key)
                            if isinstance(obj, dict):
                                for k in ("username", "name", "nickname", "login"):
                                    val = obj.get(k)
                                    if val:
                                        _save_username(chat_id, str(val))
                                        return str(val)
            elif isinstance(storage, list):
                logger.info(
                    f"[UnansweredReminder]   account.{attr} list, checking {min(len(storage), 5)} of {len(storage)} items for chat {chat_id}")
                for item in storage:
                    if isinstance(item, dict):
                        item_id = item.get("id") or item.get(
                            "chat_id") or item.get("dialog_id")
                        if str(item_id) == str(chat_id):
                            logger.info(
                                f"[UnansweredReminder]   найдена запись, поля: {list(item.keys())[:15]}")
                            for key in ("username", "user_name", "name", "nickname", "buyer_name", "seller_name", "login"):
                                val = item.get(key)
                                if val:
                                    _save_username(chat_id, str(val))
                                    return str(val)
                            for obj_key in ("user", "buyer", "customer", "interlocutor", "seller"):
                                obj = item.get(obj_key)
                                if isinstance(obj, dict):
                                    for k in ("username", "name", "nickname", "login"):
                                        val = obj.get(k)
                                        if val:
                                            _save_username(chat_id, str(val))
                                            return str(val)
                else:
                    logger.info(
                        f"[UnansweredReminder]   запись для чата {chat_id} не найдена в account.{attr}")

    api = _safe_attr(cardinal, "api")
    if api is not None:
        api_methods = [m for m in dir(api) if callable(
            getattr(api, m, None)) and not m.startswith('_')]
        logger.info(
            f"[UnansweredReminder] _fetch_dialog_username: доступные API методы ({len(api_methods)}): {api_methods[:20]}")
        for method_name in ("get_dialog", "get_chat", "get_conversation", "get_dialog_info", "get_chat_info",
                            "get_user", "get_buyer", "get_customer", "dialog_info", "chat_info",
                            "get_message", "get_dialogs_info", "get_chats_info"):
            method = _safe_attr(api, method_name)
            if method is None:
                continue
            for arg in cid_values:
                for kw in ({}, {"dialog_id": arg}, {"chat_id": arg}, {"id": arg},
                           {"dialog": arg}, {"chat": arg}):
                    try:
                        result = method(**kw) if kw else method(arg)
                        if result is not None:
                            logger.info(
                                f"[UnansweredReminder]   API {method_name}({kw or arg}) = {type(result).__name__}: {str(result)[:200]}")
                        if isinstance(result, dict):
                            for key in ("username", "user_name", "name", "nickname", "buyer_name", "seller_name"):
                                val = result.get(key)
                                if val:
                                    _save_username(chat_id, str(val))
                                    return str(val)
                            for obj_key in ("user", "buyer", "customer", "interlocutor", "seller"):
                                obj = result.get(obj_key)
                                if isinstance(obj, dict):
                                    for k in ("username", "name", "nickname", "login"):
                                        val = obj.get(k)
                                        if val:
                                            _save_username(chat_id, str(val))
                                            return str(val)
                    except Exception:
                        continue

    if api is not None:
        try:
            dialogs_list = _safe_attr(api, "get_dialogs")
            if dialogs_list and callable(dialogs_list):
                try:
                    dialogs_result = dialogs_list()
                    if dialogs_result and isinstance(dialogs_result, list):
                        for d in dialogs_result[:5]:
                            logger.info(
                                f"[UnansweredReminder]   get_dialogs() item: {type(d).__name__} = {str(d)[:100]}")
                except Exception:
                    pass
        except Exception:
            pass

    logger.info(f"[UnansweredReminder] имя не найдено для чата {chat_id}")
    return None


def _get_username(dialog: dict, chat_id: str = "") -> str:
    for key in ("username", "user_name", "buyer_name", "nickname", "login"):
        val = dialog.get(key)
        if val:
            return str(val)
    for key in ("user", "buyer", "customer"):
        obj = dialog.get(key)
        if isinstance(obj, dict):
            for k in ("username", "name", "nickname", "login", "id"):
                val = obj.get(k)
                if val:
                    return str(val)
    if chat_id:
        uname = _state.get("usernames", {}).get(chat_id)
        if uname:
            return str(uname)
    if chat_id:
        fetched = _fetch_dialog_username(chat_id)
        if fetched:
            _state.setdefault("usernames", {})[chat_id] = fetched
            _save_state()
            return fetched
    if chat_id:
        return f"Чат {chat_id}"
    return "Неизвестно"


def _get_order_id(dialog: dict) -> Optional[str]:
    for key in ("order_id", "orderId", "offer_id", "offerId", "lot_id"):
        val = dialog.get(key)
        if val is not None:
            return str(val)
    for key in ("order", "offer", "lot"):
        obj = dialog.get(key)
        if isinstance(obj, dict):
            oid = obj.get("id") or obj.get("order_id") or obj.get("offerId")
            if oid is not None:
                return str(oid)
    return None


def _classify_sender(msg: dict, dialog: dict) -> str:
    if not isinstance(msg, dict):
        return "unknown"
    t = str(msg.get("sender_type") or msg.get("type") or "").lower()
    if t in ("system", "service", "system_message", "info"):
        return "system"
    if t in ("support", "seller", "staff", "admin"):
        return "seller"
    if t in ("buyer", "user", "customer"):
        return "buyer"
    if msg.get("is_support") or msg.get("isSupport") or \
       msg.get("is_seller") or msg.get("isSeller"):
        return "seller"
    if msg.get("is_buyer") or msg.get("isBuyer") or \
       msg.get("is_customer") or msg.get("isCustomer"):
        return "buyer"
    sid = (msg.get("sender_id") or msg.get("senderId") or
           msg.get("from_id") or msg.get("userId"))
    did = (dialog.get("user_id") or dialog.get("userId") or
           dialog.get("buyer_id") or dialog.get("buyerId"))
    if sid is not None and did is not None:
        return "buyer" if str(sid) == str(did) else "seller"
    nm = str(msg.get("sender_name", msg.get("name", ""))).lower()
    if nm in ("funpay", "system", "support", "бот", "bot", "service", "admin"):
        return "system"
    return "unknown"


def _dialog_last_sender(dialog: dict) -> str:
    sv = dialog.get("last_sender") or dialog.get("lastMessageSender")
    if sv:
        s = str(sv).lower()
        if s in ("buyer", "seller", "system", "support"):
            return s if s != "support" else "seller"
    msgs = dialog.get("messages") or dialog.get("last_messages") or []
    if msgs and isinstance(msgs, list) and len(msgs) > 0:
        last = msgs[-1]
        if isinstance(last, dict):
            s = _classify_sender(last, dialog)
            if s != "unknown":
                return s
    return "unknown"


def _ignored_set() -> set:
    return set(_state.get("ignored_chats", []))


def _last_msg_time(chat_id: str) -> Optional[datetime]:
    return _parse_time(_state.get("last_message_time", {}).get(chat_id))


def _last_sender(chat_id: str) -> Optional[str]:
    return _state.get("last_sender", {}).get(chat_id)


def _set_chat(chat_id: str, sender: str, msg_time: Optional[datetime] = None) -> None:
    _state.setdefault("last_sender", {})[chat_id] = sender
    if msg_time is not None:
        _state.setdefault("last_message_time", {})[
            chat_id] = msg_time.isoformat()
    elif sender == "buyer" and chat_id not in _state.get("last_message_time", {}):
        _state.setdefault("last_message_time", {})[chat_id] = _now_iso()


def _remove_chat(chat_id: str) -> None:
    modified = False
    for key in ("last_message_time", "last_sender", "notified", "notification_count"):
        if key in _state and chat_id in _state[key]:
            del _state[key][chat_id]
            modified = True
    if modified:
        _save_state()


def _save_state() -> None:
    with _file_lock:
        _write_json(STATE_PATH, _state)


def _save_config() -> None:
    with _file_lock:
        _write_json(CONFIG_PATH, _config)


def _reset_chat_timer(chat_id: str) -> None:
    _set_chat(chat_id, "buyer", _now_utc())
    _save_state()


def _mark_answered(chat_id: str) -> None:
    _remove_chat(chat_id)


def _check_loop() -> None:
    logger.info(
        f"[UnansweredReminder] Запущен цикл проверки (интервал: {CHECK_INTERVAL_SEC} с)")
    first_pass = True
    while not _stop_event.is_set():
        try:
            if _config.get("enabled", True):
                _check_dialogs(first_pass)
            if first_pass:
                first_pass = False
                logger.info("[UnansweredReminder] Первая проверка завершена")
        except Exception as exc:
            logger.error(f"[UnansweredReminder] Ошибка в цикле: {exc}")
        _stop_event.wait(CHECK_INTERVAL_SEC)
    logger.info("[UnansweredReminder] Цикл проверки остановлен")


def _check_dialogs(first_pass: bool) -> None:
    dialogs = _get_dialogs()
    logger.info(
        f"[UnansweredReminder] _check_dialogs: {len(dialogs)} диалогов, first_pass={first_pass}")
    if not dialogs:
        logger.debug(
            "[UnansweredReminder] _get_dialogs() вернул пустой список")
        return
    now = _now_utc()
    ignored = _ignored_set()
    buyers = sellers = systems = unknowns = excluded = 0
    for dialog in dialogs:
        if not isinstance(dialog, dict):
            # API может возвращать ID диалогов (int) вместо объектов
            if isinstance(dialog, (int, str)):
                cid = str(dialog)
                ls = _state.get("last_sender", {}).get(cid)
                if ls == "buyer":
                    buyers += 1
                    _process_buyer_chat(cid, {}, now, first_pass)
                elif ls == "seller":
                    sellers += 1
                elif first_pass and cid not in _state.get("last_sender", {}):
                    _set_chat(cid, "buyer", now)
                    _save_state()
                    buyers += 1
                    logger.info(
                        f"[UnansweredReminder] начат отслеживание диалога {cid} (первый проход)")
                    uname = _fetch_dialog_username(cid)
                    if uname:
                        _state.setdefault("usernames", {})[cid] = uname
                        _save_state()
                else:
                    unknowns += 1
            else:
                unknowns += 1
            continue
        chat_id = _extract_chat_id(dialog)
        if not chat_id or chat_id in ignored:
            if chat_id and chat_id in ignored:
                logger.debug(f"[UnansweredReminder] диалог {chat_id} в игноре")
            continue
        if _config.get("exclude_closed", True):
            st = _get_order_status(dialog)
            if st in ("closed", "completed", "canceled", "cancelled"):
                _remove_chat(chat_id)
                excluded += 1
                continue
        sender = _dialog_last_sender(dialog)
        if sender == "seller":
            sellers += 1
            if _last_sender(chat_id) is not None:
                _remove_chat(chat_id)
            continue
        if sender == "system":
            systems += 1
            if _config.get("ignore_system", True):
                continue
            sender = "buyer"
        if sender == "buyer":
            buyers += 1
            _process_buyer_chat(chat_id, dialog, now, first_pass)
        else:
            unknowns += 1
    if buyers or sellers or systems or unknowns or excluded:
        logger.info(f"[UnansweredReminder] Статистика: buyers={buyers}, sellers={sellers}, "
                    f"systems={systems}, unknown={unknowns}, excluded={excluded}")


def _process_buyer_chat(chat_id: str, dialog: dict, now: datetime, first_pass: bool) -> None:
    last_time = _last_msg_time(chat_id)
    logger.debug(
        f"[UnansweredReminder] _process_buyer_chat({chat_id}): last_time={last_time}, first_pass={first_pass}")
    if last_time is None:
        _set_chat(chat_id, "buyer", now)
        _save_state()
        logger.debug(
            f"[UnansweredReminder] диалог {chat_id}: начали отсчёт, время={now}")
        return
    if first_pass:
        logger.debug(
            f"[UnansweredReminder] диалог {chat_id}: первый проход, пропускаем")
        return
    timeout_sec = _get_timeout_sec()
    elapsed = (now - last_time).total_seconds()
    logger.debug(
        f"[UnansweredReminder] диалог {chat_id}: прошло {elapsed:.0f} сек, таймаут={timeout_sec} сек")
    if elapsed < timeout_sec:
        return
    logger.info(
        f"[UnansweredReminder] диалог {chat_id}: таймаут {_format_duration(timeout_sec)} истёк (прошло {elapsed:.0f} сек) → _check_notify")
    _check_notify(chat_id, dialog, now)


def _check_notify(chat_id: str, dialog: dict, now: datetime) -> None:
    nt = _state.get("notified", {}).get(chat_id)
    last = _parse_time(nt)
    send = False
    if last is None:
        send = True
        logger.info(
            f"[UnansweredReminder] _check_notify({chat_id}): ранее не уведомляли → отправляем")
    elif _config.get("remind_again", True):
        interval = _get_remind_interval_sec()
        elapsed = (now - last).total_seconds()
        logger.debug(
            f"[UnansweredReminder] _check_notify({chat_id}): прошло {elapsed:.0f} сек с последнего уведомления, интервал={interval} сек")
        if elapsed >= interval:
            send = True
    else:
        logger.debug(
            f"[UnansweredReminder] _check_notify({chat_id}): повтор отключён, пропускаем")
    if send:
        _send_notification(chat_id, dialog)


def _send_notification(chat_id: str, dialog: dict) -> None:
    bot = _get_bot()
    admin = _admin_chat_id
    logger.debug(
        f"[UnansweredReminder] _send_notification({chat_id}): bot={'есть' if bot else 'None'}, admin={admin}")
    if not bot or not admin:
        logger.warning(
            f"[UnansweredReminder] Telegram-бот или admin_chat_id не настроены: bot={'есть' if bot else 'None'}, admin={admin}")
        return
    try:
        username = _get_username(dialog, chat_id)
        order_id = _get_order_id(dialog)
        last_time = _last_msg_time(chat_id) or _now_utc()
        wait_min = max(1, int((_now_utc() - last_time).total_seconds() / 60))
        count = _state.get("notification_count", {}).get(chat_id, 0) + 1
        _state.setdefault("notification_count", {})[chat_id] = count
        text = (
            f"⏰ Нет ответа в чате!\n"
            f"👤 Пользователь: {username}\n"
            f"🆔 ID чата: {chat_id}\n🔗 https://funpay.com/chat/?node={chat_id}\n"
            f"📦 Заказ: {order_id or 'Нет'}\n"
            f"⏳ Без ответа: {wait_min} мин."
        )
        bot.send_message(admin, text,
                         reply_markup=_build_notify_kb(chat_id))
        _state.setdefault("notified", {})[chat_id] = _now_iso()
        _save_state()
        logger.info(f"[UnansweredReminder] Напоминание по чату {chat_id}")
    except Exception as exc:
        logger.error(
            f"[UnansweredReminder] Ошибка отправки уведомления для {chat_id}: {exc}")


def _register_telegram_handlers() -> None:
    global _telegram_bot
    cardinal = _cardinal_ref
    if cardinal is None:
        return

    tg = _safe_attr(cardinal, "telegram")
    bot = _safe_attr(tg, "bot") if tg is not None else None
    if bot is None:
        logger.warning("[UnansweredReminder] Telegram bot недоступен")
        return
    _telegram_bot = bot

    if hasattr(cardinal, "register_command"):
        try:
            cardinal.register_command("unanswered", _handle_command)
            logger.info(
                "[UnansweredReminder] Команда /unanswered зарегистрирована")
        except Exception as exc:
            logger.debug(f"[UnansweredReminder] register_command: {exc}")

    if hasattr(cardinal, "register_callback_handler"):
        try:
            cardinal.register_callback_handler(
                CALLBACK_PREFIX, _handle_callback)
            logger.info(
                "[UnansweredReminder] Callback-хендлер зарегистрирован")
        except Exception as exc:
            logger.debug(
                f"[UnansweredReminder] register_callback_handler: {exc}")

    _send_tg(_admin_chat_id,
             f"✅ UnansweredReminder v{VERSION} загружен.\nИспользуйте /unanswered для управления.")


def _handle_command(cardinal: Any, message: Any, args: Any = None) -> None:
    global _admin_chat_id
    chat_id = None
    if isinstance(message, dict):
        chat_id = message.get("chat_id") or (
            message.get("chat", {}).get("id") if isinstance(message.get("chat"), dict) else None)
    else:
        chat_id = _safe_attr(message, "chat_id") or _safe_attr(
            _safe_attr(message, "chat"), "id")

    if _admin_chat_id is None and chat_id is not None:
        _admin_chat_id = chat_id
        _config["admin_chat_id"] = chat_id
        _save_config()
        logger.info(
            f"[UnansweredReminder] admin_chat_id захвачен из команды: {chat_id}")

    if chat_id is None:
        chat_id = _admin_chat_id
    if chat_id is None:
        return

    if isinstance(args, list):
        parts = args
    elif isinstance(args, str):
        parts = args.split()
    else:
        text = message.get("text", "") if isinstance(
            message, dict) else _safe_attr(message, "text", "")
        parts = text.split()[1:] if len(text.split()) > 1 else []
    _process_command(chat_id, parts)


def _on_plugin_settings_open(call: Any) -> None:
    logger.info("[UnansweredReminder] Settings button clicked!")
    try:
        bot = _telegram_bot
        if not bot:
            cardinal = _cardinal_ref
            bot = _safe_attr(_safe_attr(cardinal, "telegram"), "bot")
        if bot:
            bot.answer_callback_query(call.id)
    except Exception:
        pass
    _send_settings(call)


def _handle_callback(cardinal: Any, call: Any) -> None:
    data = call.get("data", "") if isinstance(
        call, dict) else _safe_attr(call, "data", "")
    if not data or not data.startswith(CALLBACK_PREFIX):
        return
    _dispatch_callback(data, call)


def _dispatch_callback(data: str, call: Any) -> None:
    payload = data[len(CALLBACK_PREFIX):]
    idx = payload.find("_")
    action, arg = (payload, "") if idx == - \
        1 else (payload[:idx], payload[idx + 1:])

    bot = _get_bot()
    if bot:
        try:
            cid = call.get("id") if isinstance(
                call, dict) else _safe_attr(call, "id", "0")
            if cid:
                bot.answer_callback_query(cid)
        except Exception:
            pass

    handlers = {
        "answer": _cb_answer, "ignore": _cb_ignore, "info": _cb_info,
        "settings": _cb_settings, "toggle": _cb_toggle, "timeout": _cb_timeout,
        "mode": _cb_mode, "remind": _cb_remind, "remindinterval": _cb_remind_interval,
        "exclude": _cb_exclude, "ignoresystem": _cb_ignore_sys,
        "cleanup": _cb_cleanup, "stats": _cb_stats, "close": _cb_close,
    }
    h = handlers.get(action)
    if h is None:
        return
    try:
        h(call, arg)
    except Exception as exc:
        logger.error(f"[UnansweredReminder] Callback error {data}: {exc}")


def _process_command(chat_id: int, args: list) -> None:
    bot = _telegram_bot
    if not bot:
        return
    if not args:
        return _cmd_list(chat_id)
    cmd = args[0].lower()

    if cmd == "toggle":
        _config["enabled"] = not _config.get("enabled", True)
        _save_config()
        _send_tg(
            chat_id, f'🔄 Плагин {"вкл" if _config["enabled"] else "выкл"}')
    elif cmd == "timeout" and len(args) > 1:
        try:
            raw = args[1].lower()
            if raw.endswith("s"):
                sec = int(raw[:-1])
                if sec < 10:
                    raise ValueError
            elif raw.endswith("m"):
                sec = int(raw[:-1]) * 60
                if sec < 10:
                    raise ValueError
            else:
                sec = int(raw) * 60  # по умолчанию минуты
                if sec < 10:
                    raise ValueError
            _config["timeout"] = sec
            _save_config()
            _send_tg(chat_id, f"⏱ Таймаут: {_format_duration(sec)}")
        except ValueError:
            _send_tg(chat_id, "❌ /unanswered timeout 5 (мин) или 30s (сек)")
    elif cmd == "mode" and len(args) > 1:
        if args[1] == "all":
            _config["check_all_dialogs"] = True
            _save_config()
            _send_tg(chat_id, "👁 Режим: все диалоги")
        elif args[1] in ("orders", "order"):
            _config["check_all_dialogs"] = False
            _save_config()
            _send_tg(chat_id, "👁 Режим: только заказы")
        else:
            _send_tg(chat_id, "❌ /unanswered mode all|orders")
    elif cmd == "ignore" and len(args) > 1:
        ign = _ignored_set()
        ign.add(args[1])
        _state["ignored_chats"] = list(ign)
        _save_state()
        _send_tg(chat_id, f"🔇 Чат {args[1]} в игноре")
    elif cmd == "silence":
        s = str(chat_id)
        ign = _ignored_set()
        ign.add(s)
        _state["ignored_chats"] = list(ign)
        _save_state()
        _send_tg(chat_id, "🔇 Текущий чат в игноре")
    elif cmd == "stats":
        t = len(_state.get("last_message_time", {}))
        u = sum(1 for s in _state.get(
            "last_sender", {}).values() if s == "buyer")
        i = len(_ignored_set())
        sz = os.path.getsize(STATE_PATH) if os.path.exists(STATE_PATH) else 0
        _send_tg(
            chat_id, f"📊 Чатов: {t}, неотв: {u}, игнор: {i}, размер: {sz} Б")
    elif cmd == "cleanup":
        n = _cleanup_state(force=True)
        _send_tg(chat_id, f"🗑 Удалено {n} записей")
    else:
        _send_tg(chat_id,
                 "📋 <b>UnansweredReminder</b>\n"
                 "/list — список неотвеченных\n"
                 "/cleanup — очистка",
                 parse_mode="HTML")


def _cmd_list(chat_id: int) -> None:
    bot = _telegram_bot
    if not bot:
        return
    unans = []
    ign = _ignored_set()
    for cid, s in _state.get("last_sender", {}).items():
        if s != "buyer" or cid in ign:
            continue
        lt = _last_msg_time(cid)
        if lt:
            m = int((_now_utc() - lt).total_seconds() / 60)
            unans.append((cid, m))
    if not unans:
        _send_tg(chat_id, "✅ Нет неотвеченных диалогов!")
        return
    unans.sort(key=lambda x: x[1], reverse=True)
    lines = ["⏰ <b>Неотвеченные:</b>\n"]
    for c, m in unans[:20]:
        lines.append(f"🆔 <code>{c}</code> — {m} мин.")
    if len(unans) > 20:
        lines.append(f"\n... ещё {len(unans) - 20}")
    _send_tg(chat_id, "\n".join(lines), parse_mode="HTML")


def _get_bot() -> Any:
    bot = _telegram_bot
    if bot is None:
        cardinal = _cardinal_ref
        bot = _safe_attr(_safe_attr(cardinal, "telegram"), "bot")
    return bot


def _send_tg(chat_id: int, text: str, parse_mode: Optional[str] = None, reply_markup: Any = None) -> bool:
    bot = _get_bot()
    if not bot or not chat_id:
        return False
    try:
        bot.send_message(chat_id, text, parse_mode=parse_mode,
                         reply_markup=reply_markup)
        return True
    except Exception as exc:
        logger.error(f"[UnansweredReminder] Ошибка отправки в TG: {exc}")
        return False


def _send_tg_safe(chat_id: int, text: str, parse_mode: Optional[str] = None, reply_markup: Any = None) -> None:
    try:
        _send_tg(chat_id, text, parse_mode, reply_markup)
    except Exception as exc:
        logger.error(f"[UnansweredReminder] _send_tg_safe: {exc}")


def _get_msg_chat(call) -> Optional[int]:
    if isinstance(call, dict):
        msg = call.get("message", {})
        if isinstance(msg, dict):
            ch = msg.get("chat", {})
            return ch.get("id") if isinstance(ch, dict) else None
        return None
    msg = _safe_attr(call, "message")
    if msg is not None:
        return _safe_attr(msg, "chat_id") or _safe_attr(_safe_attr(msg, "chat"), "id")
    return None


def _get_msg_id(call) -> Optional[int]:
    if isinstance(call, dict):
        msg = call.get("message", {})
        return msg.get("message_id") if isinstance(msg, dict) else None
    msg = _safe_attr(call, "message")
    return _safe_attr(msg, "message_id")


def _del_msg(call) -> None:
    bot = _get_bot()
    if not bot:
        return
    cid = _get_msg_chat(call)
    mid = _get_msg_id(call)
    if cid and mid:
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass


def _cb_answer(call, chat_id: str) -> None:
    if not chat_id:
        return
    _mark_answered(chat_id)
    _del_msg(call)
    logger.info(f"[UnansweredReminder] Чат {chat_id} помечен отвеченным")


def _cb_ignore(call, chat_id: str) -> None:
    if not chat_id:
        return
    ign = _ignored_set()
    ign.add(chat_id)
    _state["ignored_chats"] = list(ign)
    _save_state()
    _del_msg(call)
    logger.info(f"[UnansweredReminder] Чат {chat_id} в игноре")


def _cb_info(call, chat_id: str) -> None:
    if not chat_id:
        return
    lt = _last_msg_time(chat_id)
    ls = _last_sender(chat_id) or "?"
    ns = _state.get("notified", {}).get(chat_id)
    nf = _parse_time(ns)
    cnt = _state.get("notification_count", {}).get(chat_id, 0)
    ig = chat_id in _ignored_set()
    wt = "—"
    if lt:
        wt = f"{int((_now_utc() - lt).total_seconds() / 60)} мин."
    nft = "нет"
    if nf:
        nft = f"{int((_now_utc() - nf).total_seconds() / 60)} мин. назад"
    text = (
        f"📋 <b>Информация о чате</b>\n"
        f"🆔 ID: <code>{chat_id}</code>\n"
        f"👤 Отправитель: {ls}\n"
        f"⏳ Ожидание: {wt}\n"
        f"🔔 Напоминаний: {cnt}\n"
        f"📨 Последнее: {nft}\n"
        f"🚫 Игнор: {'Да' if ig else 'Нет'}"
    )
    tgt = _get_msg_chat(call) or _admin_chat_id
    if tgt:
        _send_tg(tgt, text, parse_mode="HTML")


def _cb_settings(call, _unused: str = "") -> None:
    _send_settings(call)


def _send_settings(call_or_chat) -> None:
    if isinstance(call_or_chat, (dict, object)):
        tgt = _get_msg_chat(call_or_chat)
        mid = _get_msg_id(call_or_chat)
        if not tgt:
            tgt = _admin_chat_id
    else:
        tgt, mid = call_or_chat, None
    if not tgt:
        return

    en = _config.get("enabled", True)
    to = _get_timeout_sec()
    m = "Все диалоги" if _config.get(
        "check_all_dialogs", True) else "Только заказы"
    rm = _config.get("remind_again", True)
    ri = _get_remind_interval_sec()
    ex = _config.get("exclude_closed", True)
    ig = _config.get("ignore_system", True)

    text = (
        f"⚙️ <b>Настройки UnansweredReminder</b>\n\n"
        f"🔄 {'🟢 Вкл' if en else '🔴 Выкл'}\n"
        f"⏱ Таймаут: {_format_duration(to)}\n"
        f"👁 Режим: {m}\n"
        f"🔁 Повтор: {'🟢 Вкл' if rm else '🔴 Выкл'} ({_format_duration(ri)})\n"
        f"❌ Искл. закрытые: {'Да' if ex else 'Нет'}\n"
        f"🚫 Игнор. системные: {'Да' if ig else 'Нет'}"
    )
    bot = _get_bot()
    if not bot:
        return
    if mid:
        try:
            bot.edit_message_text(
                text, tgt, mid, parse_mode="HTML", reply_markup=_build_settings_kb())
            return
        except Exception:
            pass
    try:
        bot.send_message(tgt, text, parse_mode="HTML",
                         reply_markup=_build_settings_kb())
    except Exception:
        pass


def _cb_toggle(call, _unused: str = "") -> None:
    _config["enabled"] = not _config.get("enabled", True)
    _save_config()
    _send_settings(call)


def _cb_timeout(call, _unused: str = "") -> None:
    cur = _get_timeout_sec()
    idx = 0
    for i, (_, sec) in enumerate(TIMEOUT_OPTIONS):
        if sec <= cur:
            idx = i
    _config["timeout"] = TIMEOUT_OPTIONS[(idx + 1) % len(TIMEOUT_OPTIONS)][1]
    _save_config()
    _send_settings(call)


def _cb_mode(call, _unused: str = "") -> None:
    _config["check_all_dialogs"] = not _config.get("check_all_dialogs", True)
    _save_config()
    _send_settings(call)


def _cb_remind(call, _unused: str = "") -> None:
    _config["remind_again"] = not _config.get("remind_again", True)
    _save_config()
    _send_settings(call)


def _cb_remind_interval(call, _unused: str = "") -> None:
    cur = _get_remind_interval_sec()
    idx = 0
    for i, (_, sec) in enumerate(REMIND_INTERVAL_OPTIONS):
        if sec <= cur:
            idx = i
    _config["remind_again_interval"] = REMIND_INTERVAL_OPTIONS[(
        idx + 1) % len(REMIND_INTERVAL_OPTIONS)][1]
    _save_config()
    _send_settings(call)


def _cb_exclude(call, _unused: str = "") -> None:
    _config["exclude_closed"] = not _config.get("exclude_closed", True)
    _save_config()
    _send_settings(call)


def _cb_ignore_sys(call, _unused: str = "") -> None:
    _config["ignore_system"] = not _config.get("ignore_system", True)
    _save_config()
    _send_settings(call)


def _cb_cleanup(call, _unused: str = "") -> None:
    n = _cleanup_state(force=True)
    bot = _get_bot()
    if bot:
        try:
            cid = call.get("id") if isinstance(
                call, dict) else _safe_attr(call, "id")
            if cid:
                bot.answer_callback_query(cid, text=f"🗑 Удалено {n} записей")
        except Exception:
            pass


def _cb_stats(call, _unused: str = "") -> None:
    total = len(_state.get("last_message_time", {}))
    unans = sum(1 for s in _state.get(
        "last_sender", {}).values() if s == "buyer")
    ign = len(_ignored_set())
    sz = os.path.getsize(STATE_PATH) if os.path.exists(STATE_PATH) else 0
    text = (
        f"📊 Статистика\n"
        f"Всего: {total}\n"
        f"Неотвеченных: {unans}\n"
        f"В игноре: {ign}\n"
        f"Размер: {sz} Б\n"
        f"Статус: {'✅ Вкл' if _config.get('enabled') else '❌ Выкл'}"
    )
    tgt = _get_msg_chat(call) or _admin_chat_id
    if tgt:
        _send_tg(tgt, text, parse_mode="HTML")


def _cb_close(call, _unused: str = "") -> None:
    bot = _get_bot()
    cid = _get_msg_chat(call)
    mid = _get_msg_id(call)
    if bot and cid and mid:
        try:
            bot.edit_message_text("✅ Настройки закрыты.", cid, mid)
        except Exception:
            _del_msg(call)
    else:
        _del_msg(call)


def _safe_get(obj: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if obj is None:
            return default
        if isinstance(obj, dict):
            obj = obj.get(key, default)
        else:
            obj = _safe_attr(obj, key, default)
    return obj


def on_new_message(cardinal: Any, event: Any) -> None:
    if not _config.get("enabled", True):
        return
    try:
        msg = _safe_attr(event, "message") or (
            event.get("message") if isinstance(event, dict) else None)
        if msg is None:
            msg = event
            logger.info(
                f"[UnansweredReminder] on_new_message: event не содержит message, используем сам event как msg")
        chat_id = None
        if isinstance(msg, dict):
            chat_id = msg.get("chat_id") or msg.get("chatId") or msg.get("id")
        else:
            chat_id = _safe_attr(msg, "chat_id") or _safe_attr(
                msg, "chatId") or _safe_attr(msg, "id")
        if chat_id is None:
            if isinstance(event, dict):
                chat_id = event.get("chat_id") or event.get(
                    "chatId") or event.get("id")
            else:
                chat_id = _safe_attr(event, "chat_id") or _safe_attr(
                    event, "chatId") or _safe_attr(event, "id")
        if chat_id is None:
            return
        chat_id = str(chat_id)

        sender = "unknown"
        by_bot = _safe_attr(msg, "by_bot", False)
        if by_bot:
            sender = "seller"
        else:
            author_id = _safe_attr(msg, "author_id") if not isinstance(
                msg, dict) else msg.get("author_id")
            my_id = _safe_attr(_safe_attr(cardinal, "account"), "id")
            if author_id is not None and my_id is not None and author_id == my_id:
                sender = "seller"
            else:
                if isinstance(msg, dict):
                    t = str(msg.get("sender_type")
                            or msg.get("type") or "").lower()
                else:
                    t = str(_safe_attr(msg, "sender_type")
                            or _safe_attr(msg, "type") or "").lower()
                if t in ("system", "service", "system_message"):
                    sender = "system"
                elif t in ("buyer", "user", "customer"):
                    sender = "buyer"
                else:
                    sender = "buyer"

        if sender == "buyer":
            _reset_chat_timer(chat_id)
            try:
                uname = None

                user = _safe_get(event, "user")
                if user:
                    if isinstance(user, dict):
                        for k in ("username", "name", "nickname", "login", "user_name"):
                            if user.get(k):
                                uname = str(user[k])
                                break
                    else:
                        for k in ("username", "name", "nickname", "login", "user_name"):
                            v = _safe_attr(user, k)
                            if v:
                                uname = str(v)
                                break
                if not uname:
                    for src_attr in ("sender", "author", "from_", "from", "user_data", "buyer", "customer"):
                        src = _safe_get(event, src_attr)
                        if not src:
                            continue
                        if isinstance(src, dict):
                            for k in ("username", "name", "nickname", "login", "user_name"):
                                if src.get(k):
                                    uname = str(src[k])
                                    break
                        else:
                            for k in ("username", "name", "nickname", "login", "user_name"):
                                v = _safe_attr(src, k)
                                if v:
                                    uname = str(v)
                                    break
                        if uname:
                            break

                if not uname:
                    data = _safe_get(event, "data")
                    if data:
                        for src_attr in ("user", "sender", "author", "from_", "from", "interlocutor"):
                            src = _safe_get(data, src_attr)
                            if not src:
                                continue
                            if isinstance(src, dict):
                                for k in ("username", "name", "nickname", "login", "user_name"):
                                    if src.get(k):
                                        uname = str(src[k])
                                        break
                            else:
                                for k in ("username", "name", "nickname", "login", "user_name"):
                                    v = _safe_attr(src, k)
                                    if v:
                                        uname = str(v)
                                        break
                            if uname:
                                break

                if not uname:
                    for attr in ("username", "user_name", "sender_name",
                                 "name", "chat_name", "buyer_name", "seller_name", "nickname"):
                        if isinstance(event, dict):
                            v = event.get(attr)
                        else:
                            v = _safe_attr(event, attr)
                        if v:
                            uname = str(v)
                            break

                if not uname:
                    if isinstance(msg, dict):
                        for k in ("username", "user_name", "sender_name", "name", "buyer_name", "nickname"):
                            if msg.get(k):
                                uname = str(msg[k])
                                break
                        if not uname:
                            for src_attr in ("from", "author", "sender", "user"):
                                src = msg.get(src_attr)
                                if not src:
                                    continue
                                if isinstance(src, dict):
                                    for k in ("username", "name", "nickname", "login", "user_name"):
                                        if src.get(k):
                                            uname = str(src[k])
                                            break
                                elif isinstance(src, str):
                                    uname = src
                                    break
                                if uname:
                                    break
                    else:
                        for k in ("username", "user_name", "sender_name", "name", "buyer_name", "nickname"):
                            v = _safe_attr(msg, k)
                            if v:
                                uname = str(v)
                                break
                        if not uname:
                            for src_attr in ("from", "author", "sender", "user"):
                                src = _safe_attr(msg, src_attr)
                                if not src:
                                    continue
                                if isinstance(src, dict):
                                    for k in ("username", "name", "nickname", "login", "user_name"):
                                        if src.get(k):
                                            uname = str(src[k])
                                            break
                                elif isinstance(src, str):
                                    uname = src
                                    break
                                else:
                                    for k in ("username", "name", "nickname", "login", "user_name"):
                                        v = _safe_attr(src, k)
                                        if v:
                                            uname = str(v)
                                            break
                                if uname:
                                    break

                if not uname:
                    data = _safe_get(msg, "data")
                    if data:
                        for src_attr in ("user", "sender", "author", "from_", "from", "interlocutor"):
                            src = _safe_get(data, src_attr)
                            if not src:
                                continue
                            if isinstance(src, dict):
                                for k in ("username", "name", "nickname", "login", "user_name"):
                                    if src.get(k):
                                        uname = str(src[k])
                                        break
                            else:
                                for k in ("username", "name", "nickname", "login", "user_name"):
                                    v = _safe_attr(src, k)
                                    if v:
                                        uname = str(v)
                                        break
                            if uname:
                                break

                if uname:
                    _state.setdefault("usernames", {})[chat_id] = str(uname)
                    _save_state()
                    logger.info(
                        f"[UnansweredReminder] ✅ Сохранено имя для чата {chat_id}: {uname}")
                else:
                    ev_type = type(event).__name__
                    ev_id = id(event)
                    try:
                        ev_dir = str(
                            [a for a in dir(event) if not a.startswith('_')][:15])
                    except Exception:
                        ev_dir = "?dir?"
                    msg_type = type(msg).__name__
                    try:
                        msg_repr = repr(msg)[:200]
                    except Exception:
                        msg_repr = "?repr?"
                    logger.info(
                        f"[UnansweredReminder] ❌ НЕ удалось извлечь имя для чата {chat_id}: "
                        f"event={ev_type}(id={ev_id}), attrs={ev_dir}, "
                        f"msg_type={msg_type}, msg={msg_repr}")
            except Exception as e:
                logger.warning(
                    f"[UnansweredReminder] Ошибка сохранения имени: {e}")
            logger.info(
                f"[UnansweredReminder] Сообщение от покупателя в {chat_id} — таймер сброшен")
        elif sender == "seller":
            _mark_answered(chat_id)
            logger.info(
                f"[UnansweredReminder] Сообщение от продавца в {chat_id} — таймер снят")
        elif sender == "system" and _config.get("ignore_system", True):
            pass
    except Exception as exc:
        logger.error(f"[UnansweredReminder] Ошибка on_new_message: {exc}")


def _cleanup_loop() -> None:
    interval = _config.get("cleanup_interval_minutes", 60) * 60
    _stop_event.wait(interval)
    while not _stop_event.is_set():
        try:
            _cleanup_state(force=False)
        except Exception as exc:
            logger.error(f"[UnansweredReminder] Ошибка очистки: {exc}")
        _stop_event.wait(interval)


def _cleanup_state(force: bool = False) -> int:
    if not force:
        lc = _state.get("last_cleanup")
        if lc:
            dt = _parse_time(lc)
            if dt:
                interval = _config.get("cleanup_interval_minutes", 60)
                if (_now_utc() - dt).total_seconds() < interval * 60:
                    return 0
    deleted = 0
    now = _now_utc()
    max_age = timedelta(hours=_config.get("cleanup_max_age_hours", 24))
    max_chats = _config.get("cleanup_max_chats", 500)

    for cid in list(_state.get("last_message_time", {}).keys()):
        ts = _state["last_message_time"].get(cid)
        if ts:
            t = _parse_time(ts)
            if t and (now - t) > max_age:
                _remove_chat(cid)
                deleted += 1

    tm = _state.get("last_message_time", {})
    if len(tm) > max_chats:
        sorted_c = sorted(
            tm.items(), key=lambda x: _parse_time(x[1]) or datetime.min)
        for cid, _ in sorted_c[:len(sorted_c) - max_chats]:
            _remove_chat(cid)
            deleted += 1

    for cid, s in list(_state.get("last_sender", {}).items()):
        if s in ("seller", "system"):
            _remove_chat(cid)
            deleted += 1

    _state["last_cleanup"] = _now_iso()
    _save_state()
    if deleted > 0 or force:
        logger.info(f"[UnansweredReminder] Очистка: удалено {deleted} записей")
    return deleted


def _on_pre_init(cardinal: Any) -> None:
    global _telegram_bot, _config, _state, _cardinal_ref, _admin_chat_id
    try:
        if _cardinal_ref is None:
            _cardinal_ref = cardinal
        _ensure_storage_dir()
        if not _config:
            _config = _load_json(CONFIG_PATH, DEFAULT_CONFIG)
        if not _state:
            _state = _load_json(STATE_PATH, DEFAULT_STATE)
        if _admin_chat_id is None:
            _admin_chat_id = _resolve_admin_chat_id()
        if _admin_chat_id is None:
            logger.warning(
                "[UnansweredReminder] admin_chat_id = None — уведомления НЕ БУДУТ отправляться!")
        else:
            logger.info(
                f"[UnansweredReminder] admin_chat_id = {_admin_chat_id}")

        tg = cardinal.telegram
        if tg is None:
            logger.warning("[UnansweredReminder] telegram None в PRE_INIT")
            return
        bot = _safe_attr(tg, "bot")
        if not bot:
            logger.warning("[UnansweredReminder] bot None в PRE_INIT")
            return
        _telegram_bot = bot
        logger.info("[UnansweredReminder] _telegram_bot установлен в PRE_INIT")

        if hasattr(cardinal, "register_command"):
            try:
                cardinal.register_command("unanswered", _handle_command)
                logger.info(
                    "[UnansweredReminder] Команда /unanswered зарегистрирована (cardinal)")
            except Exception as exc:
                logger.debug(f"[UnansweredReminder] register_command: {exc}")

        def _handle_direct_unanswered(message: Any) -> None:
            global _admin_chat_id
            try:
                msg_chat_id = None
                if isinstance(message, dict):
                    msg_chat_id = message.get("chat_id") or (
                        message.get("chat", {}).get("id") if isinstance(message.get("chat"), dict) else None)
                else:
                    msg_chat_id = _safe_attr(message, "chat_id") or _safe_attr(
                        _safe_attr(message, "chat"), "id")
                if not msg_chat_id:
                    return

                if _admin_chat_id is None:
                    _admin_chat_id = msg_chat_id
                    _config["admin_chat_id"] = msg_chat_id
                    _save_config()
                    logger.info(
                        f"[UnansweredReminder] admin_chat_id захвачен из /unanswered: {msg_chat_id}")

                text = ""
                if isinstance(message, dict):
                    text = message.get("text", "")
                else:
                    text = _safe_attr(message, "text", "") or ""
                parts = text.split()[1:] if len(text.split()) > 1 else []
                _process_command(msg_chat_id, parts)
            except Exception as exc:
                logger.error(
                    f"[UnansweredReminder] _handle_direct_unanswered: {exc}")

        def _filter_unanswered(msg: Any) -> bool:
            text = _safe_attr(msg, "text") if not isinstance(
                msg, dict) else msg.get("text", "")
            return bool(text) and text.startswith("/unanswered")

        bot.message_handlers.insert(0, {
            'function': _handle_direct_unanswered,
            'filters': {'func': _filter_unanswered}
        })
        logger.info(
            "[UnansweredReminder] Команда /unanswered зарегистрирована (напрямую в telebot)")

        SHORT_COMMANDS = {
            "/list": "", "/cleanup": "cleanup",
        }

        def _make_short_handler(cmd_arg: str):
            def handler(message: Any) -> None:
                global _admin_chat_id
                try:
                    msg_chat_id = None
                    if isinstance(message, dict):
                        msg_chat_id = message.get("chat_id") or (
                            message.get("chat", {}).get("id") if isinstance(message.get("chat"), dict) else None)
                    else:
                        msg_chat_id = _safe_attr(message, "chat_id") or _safe_attr(
                            _safe_attr(message, "chat"), "id")
                    if not msg_chat_id:
                        return
                    if _admin_chat_id is None:
                        _admin_chat_id = msg_chat_id
                        _config["admin_chat_id"] = msg_chat_id
                        _save_config()
                        logger.info(
                            f"[UnansweredReminder] admin_chat_id захвачен из /list: {msg_chat_id}")
                    msg_text = ""
                    if isinstance(message, dict):
                        msg_text = message.get("text", "")
                    else:
                        msg_text = _safe_attr(message, "text", "") or ""
                    extra_args = msg_text.split()[1:] if len(
                        msg_text.split()) > 1 else []
                    all_args = (cmd_arg.split()
                                if cmd_arg else []) + extra_args
                    _process_command(msg_chat_id, all_args)
                except Exception as exc:
                    logger.error(
                        f"[UnansweredReminder] short cmd error: {exc}")
            return handler

        def _make_filter(txt):
            return lambda msg: bool(
                (_safe_attr(msg, "text") if not isinstance(
                    msg, dict) else msg.get("text", ""))
            ) and (_safe_attr(msg, "text") if not isinstance(msg, dict) else msg.get("text", "")).startswith(txt)

        for cmd_text, cmd_arg in SHORT_COMMANDS.items():
            bot.message_handlers.insert(0, {
                'function': _make_short_handler(cmd_arg),
                'filters': {'func': _make_filter(cmd_text)}
            })
            logger.info(
                f"[UnansweredReminder] Команда {cmd_text} зарегистрирована")

        if hasattr(cardinal, "register_callback_handler"):
            try:
                cardinal.register_callback_handler(
                    CALLBACK_PREFIX, _handle_callback)
                logger.info(
                    "[UnansweredReminder] Callback-хендлер ur_* зарегистрирован")
            except Exception as exc:
                logger.debug(
                    f"[UnansweredReminder] register_callback_handler: {exc}")

        def _catch_all_handler(call: Any) -> None:
            data = _safe_attr(call, "data") or ""
            if data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}:"):
                try:
                    bot.answer_callback_query(call.id)
                except Exception as e:
                    logger.error(f"[UnansweredReminder] answer: {e}")
                _send_settings(call)
            elif data.startswith(CALLBACK_PREFIX):
                _dispatch_callback(data, call)

        bot.callback_query_handlers.insert(0, {
            'function': _catch_all_handler,
            'filters': {'func': lambda c: (
                _safe_attr(c, "data", "").startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}:") or
                _safe_attr(c, "data", "").startswith(CALLBACK_PREFIX)
            )}
        })
        logger.info(
            "[UnansweredReminder] Хендлер вставлен в начало списка telebot")

        if not _stop_event.is_set():
            _stop_event.clear()
            t1 = threading.Thread(target=_check_loop,
                                  name="UR-Check", daemon=True)
            t1.start()
            t2 = threading.Thread(target=_cleanup_loop,
                                  name="UR-Cleanup", daemon=True)
            t2.start()
            logger.info("[UnansweredReminder] Фоновые потоки запущены")

        logger.info(
            f"[UnansweredReminder] Плагин v{VERSION} полностью инициализирован")
    except Exception as exc:
        logger.error(f"[UnansweredReminder] PRE_INIT error: {exc}")


def _on_post_init(cardinal: Any) -> None:
    global _admin_chat_id
    if _admin_chat_id is not None:
        return
    logger.info(
        "[UnansweredReminder] POST_INIT: повторная попытка определить admin_chat_id")

    cid = _config.get("admin_chat_id")
    if cid is not None:
        try:
            _admin_chat_id = int(cid)
            logger.info(
                f"[UnansweredReminder] POST_INIT: admin_chat_id = {_admin_chat_id} из config.json")
            return
        except (ValueError, TypeError):
            pass

    cardinal_ref = _cardinal_ref
    if cardinal_ref is not None:
        cfg = _safe_attr(cardinal_ref, "config", {})
        if isinstance(cfg, dict):
            for key in ("admin_id", "owner_id", "admin_chat_id", "master_id"):
                val = cfg.get(key)
                if val is not None:
                    try:
                        _admin_chat_id = int(val)
                        _config["admin_chat_id"] = _admin_chat_id
                        _save_config()
                        logger.info(
                            f"[UnansweredReminder] POST_INIT: admin_chat_id = {_admin_chat_id} из cardinal.config")
                        return
                    except (ValueError, TypeError):
                        pass

    logger.info(
        "[UnansweredReminder] POST_INIT: admin_chat_id не найден. Отправьте /list боту в Telegram для авто-захвата.")


def init(cardinal: Any, *args: Any) -> None:
    global _cardinal_ref
    if _cardinal_ref is None:
        _cardinal_ref = cardinal
    logger.info(f"[UnansweredReminder] init() вызван v{VERSION}")


def unbind(cardinal: Any, *args: Any) -> None:
    logger.info("[UnansweredReminder] Остановка плагина...")
    _stop_event.set()
    _save_state()
    logger.info("[UnansweredReminder] Плагин остановлен")


BIND_TO_PRE_INIT = [_on_pre_init]
BIND_TO_POST_INIT = [_on_post_init]
BIND_TO_NEW_ORDER = []
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_DELETE = None
