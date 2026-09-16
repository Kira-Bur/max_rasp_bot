import json
import asyncio
import os
import logging
import hashlib
import re
from datetime import datetime, timedelta, timezone
from maxapi import Bot, Dispatcher
from maxapi.types import Command, MessageCreated, InputMedia
from maxapi.enums import HTTPMethod

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ScheduleBot")

BOT_TOKEN = ""

BASE_PATH = os.path.dirname(os.path.abspath(__file__))
SENT_HASHES_PATH = os.path.join(BASE_PATH, "sent_hashes.json")
OUTPUT_PATH = os.path.join(BASE_PATH, "output")
MSK = timezone(timedelta(hours=3))

CHAT_1_ID = -78375979315423
CHAT_2_PSSZ_ID = -78376244539615
CHAT_2_PPKRS_ID = -78376601907423
CHAT_3_ID = -78376649224415
CHAT_ZFO_ID = -78376763125983

PIN_TOMORROW_AFTER_HOUR = 16

CORPUSES = {
    'zfo': {
        'name': 'Заочное отделение (ЗФО)',
        'folder_keywords': ['зфо', 'заоч', 'заочное'],
        'display_name': 'Заочное отделение (ЗФО)',
        'chats': [{'chat_id': CHAT_ZFO_ID, 'name': 'Чат ЗФО', 'filter_keywords': None}]
    },
    'corpus1': {
        'name': 'корпус №1 (ФМПК)',
        'folder_keywords': ['корпус 1', 'фмпк', 'корпус№1', 'корпус №1', '1 корпус', '1корпус'],
        'display_name': '1 корпус (ФМПК)',
        'chats': [{'chat_id': CHAT_1_ID, 'name': 'Основной чат 1 корпуса', 'filter_keywords': None}]
    },
    'corpus2': {
        'name': 'корпус №2 (ФМП)',
        'folder_keywords': ['корпус 2', 'корпус№2', 'корпус №2', '2 корпус', '2корпус', 'птф'],
        'display_name': '2 корпус (ФМП)',
        'chats': [
            {'chat_id': CHAT_2_PSSZ_ID, 'name': 'Чат ПССЗ/ПСЗ', 'filter_keywords': ['пссз', 'псз', 'ппссз', 'ппсз']},
            {'chat_id': CHAT_2_PPKRS_ID, 'name': 'Чат ППКРС', 'filter_keywords': ['ппкрс', 'пкрс']}
        ]
    },
    'corpus3': {
        'name': 'корпус №3',
        'folder_keywords': ['корпус 3', 'корпус№3', 'корпус №3', '3 корпус', '3корпус'],
        'display_name': '3 корпус',
        'chats': [{'chat_id': CHAT_3_ID, 'name': 'Основной чат 3 корпуса', 'filter_keywords': None}]
    }
}

bot = Bot(token=BOT_TOKEN)

def load_sent_hashes():
    if not os.path.exists(SENT_HASHES_PATH):
        return {'_pinned': {}}
    try:
        with open(SENT_HASHES_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if '_pinned' not in data:
            data['_pinned'] = {}
        return data
    except Exception as e:
        logger.error(f"Ошибка чтения базы: {e}")
        return {'_pinned': {}}


def save_sent_hashes(data):
    try:
        with open(SENT_HASHES_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Ошибка сохранения базы: {e}")


def get_target_corpus_key(filename: str):
    fname_lower = filename.lower()
    for c_key, c_info in CORPUSES.items():
        if any(k.lower() in fname_lower for k in c_info['folder_keywords']):
            return c_key
    return None


def extract_date_from_filename(filename):
    today = datetime.now(MSK).date()
    current_year = today.year

    patterns = [
        (r'(\d{1,2})[\.\s]+(\d{1,2})[\.\s]+(\d{2,4})', 'full'),
        (r'(\d{1,2})[\.\s]+(\d{1,2})(?![\.\d])', 'no_year'),
    ]
    for pattern, p_type in patterns:
        match = re.search(pattern, filename)
        if match:
            try:
                if p_type == 'full':
                    d, m, y = map(int, match.groups())
                    if y < 100:
                        y += 2000
                    file_date = datetime(y, m, d).date()
                else:
                    d, m = map(int, match.groups())
                    file_date = datetime(current_year, m, d).date()
                    if file_date < today - timedelta(days=30):
                        file_date = datetime(current_year + 1, m, d).date()
                days_diff = (file_date - today).days
                return days_diff, file_date
            except Exception:
                continue
    return 9999, None


def extract_message_id(obj):
    if obj is None:
        return None
    if isinstance(obj, (str, int)):
        return str(obj)
    if isinstance(obj, (list, tuple)):
        for item in obj:
            res = extract_message_id(item)
            if res:
                return res
        return None

    if hasattr(obj, 'model_dump') and callable(obj.model_dump):
        try:
            obj = obj.model_dump()
        except Exception:
            pass
    elif hasattr(obj, 'dict') and callable(obj.dict):
        try:
            obj = obj.dict()
        except Exception:
            pass

    if isinstance(obj, dict):
        for k in ['message_id', 'id', 'mid', 'msg_id']:
            if k in obj and obj[k] is not None:
                return str(obj[k])
        for parent_key in ['message', 'body', 'data', 'result', 'payload']:
            if parent_key in obj and obj[parent_key]:
                res = extract_message_id(obj[parent_key])
                if res:
                    return res
        for v in obj.values():
            if isinstance(v, (dict, list, tuple)):
                res = extract_message_id(v)
                if res:
                    return res

    for attr in ['message_id', 'id', 'mid', 'msg_id']:
        val = getattr(obj, attr, None)
        if val is not None:
            return str(val)

    for parent_attr in ['message', 'body', 'data', 'result', 'payload']:
        val = getattr(obj, parent_attr, None)
        if val is not None:
            res = extract_message_id(val)
            if res:
                return res

    return None


async def pin_message_in_chat(chat_id: int, message_id: str):
    try:
        if hasattr(bot, 'pin_message'):
            await bot.pin_message(chat_id=chat_id, message_id=message_id)
            logger.info(f"Сообщение {message_id} закреплено через bot.pin_message")
            return True
    except Exception as e:
        logger.debug(f"bot.pin_message вызвать не удалось: {e}")

    endpoints = [
        (HTTPMethod.POST, "/messages/pin", {"params": {"chat_id": chat_id, "message_id": message_id}}),
        (HTTPMethod.PUT, f"/chats/{chat_id}/pin", {"params": {"message_id": message_id}}),
        (HTTPMethod.POST, f"/chats/{chat_id}/pin", {"params": {"message_id": message_id}}),
    ]
    for method, path, kwargs in endpoints:
        try:
            await bot.request(method=method, path=path, **kwargs)
            logger.info(f"Сообщение {message_id} закреплено через {method.value} {path}")
            return True
        except Exception as e:
            logger.debug(f"Эндпоинт {method.value} {path} не ответил: {e}")

    logger.error(f"Не удалось закрепить сообщение {message_id} в чате {chat_id}")
    return False


async def unpin_message_in_chat(chat_id: int):
    try:
        if hasattr(bot, 'unpin_message'):
            await bot.unpin_message(chat_id=chat_id)
            logger.info(f"Сообщение откреплено в чате {chat_id}")
            return True
    except Exception:
        pass

    endpoints = [
        (HTTPMethod.DELETE, "/messages/unpin", {"params": {"chat_id": chat_id}}),
        (HTTPMethod.DELETE, f"/chats/{chat_id}/pin", {}),
    ]
    for method, path, kwargs in endpoints:
        try:
            await bot.request(method=method, path=path, **kwargs)
            logger.info(f"Сообщение откреплено в чате {chat_id}")
            return True
        except Exception:
            pass
    return False


def select_file_to_pin(chat_files, now_msk, output_path=None):
    today = now_msk.date()
    tomorrow = today + timedelta(days=1)
    prefer_tomorrow = now_msk.hour >= PIN_TOMORROW_AFTER_HOUR

    # Сортируем список файлов по времени их изменения (mtime),
    # чтобы более свежие версии файлов (если они перезаписывались или добавлялись)
    # обрабатывались последними и гарантированно перезаписывали переменные.
    if output_path and os.path.exists(output_path):
        try:
            chat_files = sorted(
                chat_files,
                key=lambda f: os.path.getmtime(os.path.join(output_path, f))
            )
        except Exception as e:
            logger.error(f"Ошибка сортировки файлов по времени изменения: {e}")

    today_file = None
    tomorrow_file = None
    future_candidates = []

    for filename in chat_files:
        days_diff, file_date = extract_date_from_filename(filename)
        if file_date is None:
            continue
        if file_date == today:
            today_file = filename
        elif file_date == tomorrow:
            tomorrow_file = filename
        elif file_date > tomorrow:
            future_candidates.append((days_diff, filename))

    future_candidates.sort(key=lambda x: x[0])

    if prefer_tomorrow:
        if tomorrow_file:
            return tomorrow_file, tomorrow
        if future_candidates:
            return future_candidates[0][1], None
    else:
        if today_file:
            return today_file, today

    return None, None


async def check_and_update_pins(sent_hashes):
    logger.info("Проверка закреплений")
    now_msk = datetime.now(MSK)
    logger.info(
        f"Текущее время МСК: {now_msk.strftime('%d.%m.%Y %H:%M')} | "
        f"режим: {'на ЗАВТРА (после 16:00)' if now_msk.hour >= PIN_TOMORROW_AFTER_HOUR else 'на СЕГОДНЯ (до 16:00)'}"
    )

    for corpus_key, corpus_info in CORPUSES.items():
        for chat_config in corpus_info['chats']:
            chat_id_num = chat_config['chat_id']
            chat_id = str(chat_id_num)
            chat_name = chat_config['name']

            if chat_id not in sent_hashes or not isinstance(sent_hashes[chat_id], dict):
                continue

            if os.path.exists(OUTPUT_PATH):
                all_files = [f for f in os.listdir(OUTPUT_PATH) if os.path.isfile(os.path.join(OUTPUT_PATH, f))]
            else:
                all_files = []

            chat_files = []
            for fname in all_files:
                if not fname.lower().endswith(('.docx', '.xlsx', '.xls', '.doc', '.pdf', '.png', '.jpg', '.jpeg')):
                    continue
                if get_target_corpus_key(fname) != corpus_key:
                    continue
                if chat_config['filter_keywords']:
                    is_general = 'общее' in fname.lower()
                    is_chat = any(k.lower() in fname.lower() for k in chat_config['filter_keywords'])
                    if not is_general and not is_chat:
                        continue
                chat_files.append(fname)

            if not chat_files:
                logger.info(f"[{chat_name}] нет подходящих файлов для закрепления")
                continue

            best_file, best_date = select_file_to_pin(chat_files, now_msk, OUTPUT_PATH)
            if not best_file:
                logger.info(f"[{chat_name}] не найден актуальный файл для закрепления в данном временном интервале")
                continue

            file_data = sent_hashes[chat_id].get(best_file)
            if not file_data:
                logger.info(f"[{chat_name}] файл {best_file} ещё не отправлен, закреплять нечего")
                continue

            msg_id = file_data.get('message_id') if isinstance(file_data, dict) else None
            if not msg_id:
                logger.warning(f"[{chat_name}] нет message_id для файла {best_file}")
                continue

            pinned_data = sent_hashes.get('_pinned', {})
            current_pinned_msg_id = pinned_data.get(chat_id)

            if current_pinned_msg_id == msg_id:
                logger.info(f"[{chat_name}] уже закреплён актуальный файл {best_file} (msg_id: {msg_id})")
                continue

            if current_pinned_msg_id:
                await unpin_message_in_chat(chat_id_num)
                await asyncio.sleep(1)

            if await pin_message_in_chat(chat_id_num, msg_id):
                if '_pinned' not in sent_hashes:
                    sent_hashes['_pinned'] = {}
                sent_hashes['_pinned'][chat_id] = msg_id
                save_sent_hashes(sent_hashes)
                date_str = best_date.strftime('%d.%m.%Y') if best_date else '—'
                logger.info(
                    f"[{chat_name}] закреплён файл {best_file} "
                    f"(msg_id: {msg_id}, дата: {date_str})"
                )


async def process_file_sending(chat_id, file_path, caption, filename, sent_hashes):
    chat_key = str(chat_id)

    if chat_key not in sent_hashes or not isinstance(sent_hashes[chat_key], dict):
        sent_hashes[chat_key] = {}

    if filename in sent_hashes[chat_key]:
        logger.info(f"Пропуск (уже отправлялся): {filename} -> чат {chat_id}")
        return None

    try:
        media = InputMedia(file_path)
        resp = await bot.send_message(chat_id=chat_id, text=caption, attachments=[media])
        msg_id = extract_message_id(resp)

        if not msg_id:
            logger.warning(f"Не удалось распознать message_id из ответа: {resp!r}")

        sent_hashes[chat_key][filename] = {'message_id': msg_id}
        save_sent_hashes(sent_hashes)
        logger.info(f"Отправлено: {filename} -> Чат {chat_id} (msg_id: {msg_id})")
        return msg_id
    except Exception as e:
        logger.error(f"Ошибка отправки {filename} в {chat_id}: {e}")
        return None


async def run_script():
    sent_hashes = load_sent_hashes()

    if not os.path.exists(OUTPUT_PATH):
        logger.warning(f"Папка {OUTPUT_PATH} не найдена!")
        return

    for cid, files in list(sent_hashes.items()):
        if cid.startswith('_') or not isinstance(files, dict):
            continue
        for fname in list(files.keys()):
            if not os.path.exists(os.path.join(OUTPUT_PATH, fname)):
                logger.info(f"Удален несуществующий файл из базы: {fname}")
                del sent_hashes[cid][fname]
    save_sent_hashes(sent_hashes)

    all_files = [
        f for f in os.listdir(OUTPUT_PATH)
        if f.lower().endswith(('.docx', '.xlsx', '.xls', '.doc', '.pdf', '.png', '.jpg', '.jpeg'))
    ]

    global_chats = []
    for corp in CORPUSES.values():
        for ch in corp['chats']:
            if ch['chat_id'] not in [gc['chat_id'] for gc in global_chats]:
                global_chats.append(ch)

    for filename in all_files:
        file_path = os.path.join(OUTPUT_PATH, filename)
        days_diff, file_date = extract_date_from_filename(filename)

        c_key = get_target_corpus_key(filename)

        if c_key:
            c_info = CORPUSES[c_key]
            for chat_cfg in c_info['chats']:
                if chat_cfg['filter_keywords']:
                    is_general = 'общее' in filename.lower()
                    is_chat = any(k.lower() in filename.lower() for k in chat_cfg['filter_keywords'])
                    if not is_general and not is_chat:
                        continue

                caption = f"🏫 {c_info['display_name']}"
                if file_date:
                    caption += f"\n📅 {file_date.strftime('%d.%m.%Y')}"

                await process_file_sending(chat_cfg['chat_id'], file_path, caption, filename, sent_hashes)
        else:
            logger.info(f"Обнаружен общий файл (без корпуса): {filename}")
            for chat_cfg in global_chats:
                await process_file_sending(
                    chat_cfg['chat_id'], file_path,
                    f"📢 Общая информация\n📄 {filename}",
                    filename, sent_hashes
                )

    await check_and_update_pins(sent_hashes)


async def main():
    try:
        await run_script()
    finally:
        if hasattr(bot, 'close') and callable(bot.close):
            await bot.close()
        elif hasattr(bot, 'session') and hasattr(bot.session, 'close'):
            await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
