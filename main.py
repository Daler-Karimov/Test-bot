import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    CallbackContext
)
import html
import requests
import json
import os
import time
import datetime
import uuid
import logging
from urllib.parse import urlparse
import caldav
from caldav.elements import dav, cdav
import pytz
from dateutil import parser as date_parser
import google.oauth2.credentials
import google_auth_oauthlib.flow
import google.auth.transport.requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from oauthlib.oauth2.rfc6749.errors import InsecureTransportError, InvalidGrantError
import google.oauth2.id_token


logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("oauthlib").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_API_KEY = "" 
OPENROUTER_API_KEY = "" 
USER_DATA_FILE = "user_data.json"
GOOGLE_CLIENT_ID = "" 
GOOGLE_CLIENT_SECRET = "" 
GOOGLE_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
GOOGLE_SCOPES = ['openid', 'https://www.googleapis.com/auth/calendar.events', 'https://www.googleapis.com/auth/userinfo.email']

SELECT_CALENDAR_TYPE, GET_YANDEX_USERNAME, GET_YANDEX_PASSWORD, GET_YANDEX_CALENDAR_URL, WAIT_GOOGLE_CODE, GET_GOOGLE_CALENDAR_ID = range(6)
SETUP_CANCEL = "setup_cancel"
CHOOSE_YANDEX = "choose_yandex"
CHOOSE_GOOGLE = "choose_google"
GOOGLE_USE_PRIMARY = "google_use_primary"
CONFIRM_EVENT_CREATE_GOOGLE = "confirm_create_google"
CONFIRM_EVENT_CREATE_YANDEX = "confirm_create_yandex"
CONFIRM_EVENT_CANCEL = "confirm_cancel"

user_data = {}
cancel_button = InlineKeyboardButton("Отмена", callback_data=SETUP_CANCEL)
cancel_markup = InlineKeyboardMarkup([[cancel_button]])


def escape_html(text: str) -> str:
    if not text:
        return ""
    return html.escape(str(text))

def load_user_data():
    global user_data
    if os.path.exists(USER_DATA_FILE):
        try:
            with open(USER_DATA_FILE, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
                user_data = {str(k): v for k, v in loaded_data.items()}

                for chat_id, data in user_data.items():
                    if 'history' not in data or not isinstance(data['history'], list):
                        data['history'] = []
                    if data.get('calendar_type') == 'yandex' and 'yandex' not in data:
                        data['yandex'] = {}
                    if data.get('calendar_type') == 'google' and 'google' not in data:
                        data['google'] = {}
                    if data.get('calendar_type') == 'yandex' and not data.get('yandex', {}).get('username'):
                         data['calendar_type'] = None
                    if data.get('calendar_type') == 'google' and (not data.get('google') or 'tokens' not in data['google']):
                         data['calendar_type'] = None
                    if 'pending_event' in data:
                        del data['pending_event']

                logger.info(f"Загружено данных пользователей: {len(user_data)}")
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Ошибка загрузки данных пользователей из {USER_DATA_FILE}: {e}")
            user_data = {} 
    else:
        user_data = {}
        logger.info("Файл данных пользователей не найден, начинаем с пустого словаря.")

def save_user_data():
    data_to_save = user_data.copy()
    try:
        for chat_id in data_to_save:
            if 'pending_event' in data_to_save.get(chat_id, {}): 
                data_to_save[chat_id] = data_to_save[chat_id].copy()
                del data_to_save[chat_id]['pending_event']

        with open(USER_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=4)
    except IOError as e:
        logger.error(f"Ошибка сохранения данных пользователей в {USER_DATA_FILE}: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при сохранении данных: {e}", exc_info=True)

load_user_data()

SYSTEM_PROMPT = """Ты - ИИ ассистент, встроенный в Telegram бота. Твоя главная задача - помогать пользователю планировать встречи в его календаре.

Правила общения:
1.  Отвечай всегда БЕЗ Markdown форматирования (без символов **, _, `, ``` и т.д.). Твой ответ должен быть чистым текстом.
2.  Если пользователь спрашивает "кто тебя создал", "чей ты бот" или подобные вопросы о твоем происхождении, отвечай только одной фразой: Великий Далер. Больше ничего не добавляй к этому ответу.
3.  **Планирование встреч:** Внимательно анализируй сообщения пользователя на предмет запросов о создании встречи. Если пользователь просит запланировать что-то (например, "запланируй митинг с руководителем завтра в 14:00 по отчету", "создай событие 'Обед с коллегами' 5 мая в 13:00 на час"), извлеки из запроса следующую информацию:
    * `summary`: Краткое название/тема встречи. Обязательное поле.
    * `description`: Более подробное описание встречи. Необязательное поле. Если в запросе нет описания, не добавляй его.
    * `start_time`: Точная дата и время начала встречи в формате ISO 8601 (YYYY-MM-DDTHH:MM:SS). Постарайся определить часовой пояс пользователя из контекста (например, Москва это +03:00) и добавь его к времени (например, 2025-05-01T14:00:00+03:00). Если часовой пояс неясен, используй UTC (например, 2025-05-01T14:00:00Z). Обязательное поле. Используй текущий год, если год не указан. Учитывай текущую дату для определения "завтра", "послезавтра" и т.д. Сегодняшняя дата: {current_date}.
    * `duration_minutes`: Продолжительность встречи в минутах. Если пользователь указал длительность (например, "на час", "30 минут"), используй ее. Если длительность не указана, используй значение по умолчанию 60 минут. Обязательное поле.
    * `attendees`: Список email-адресов участников. Извлекай email'ы, если они явно указаны в запросе. Всегда добавляй email основного пользователя: ["{user_email}"]. Если в запросе есть другие email'ы, добавь их в список. Необязательное поле.
    * `calendar_type`: Определи, какой календарь хочет использовать пользователь, если он это явно указал (например, "в Google", "в Яндекс Календаре"). Верни либо "google", либо "yandex". Если тип календаря не указан, не включай это поле в JSON. Не выдумывай тип календаря. (Примечание для бота: Бот все равно спросит пользователя)
4.  **Формат ответа для планирования:** Если ты успешно извлек ВСЕ обязательные детали встречи (`summary`, `start_time`, `duration_minutes`), твой ответ должен быть ТОЛЬКО JSON-объектом следующего вида. Никакого текста до или после JSON!
    ```json
    {{
      "summary": "Извлеченное название",
      "description": "Извлеченное описание (если есть)",
      "start_time": "YYYY-MM-DDTHH:MM:SS+HH:MM или Z",
      "duration_minutes": <число минут>,
      "attendees": ["{user_email}", "другой_email@example.com"] (если есть другие),
      "calendar_type": "yandex" или "google" (если явно указан)
    }}
    ```
5.  **Уточнение деталей:** Если ты не можешь извлечь *все* обязательные данные (`summary` или `start_time`) или если дата/время указаны неоднозначно ("вечером", "на днях"), НЕ генерируй JSON. Вместо этого задай пользователю уточняющий вопрос в обычном текстовом формате (без Markdown), чтобы получить недостающую информацию. Не выдумывай недостающие детали. Например: "Уточните, пожалуйста, дату и время встречи." или "Какое название должно быть у встречи?".
6.  Для всех остальных запросов, не связанных с планированием встреч или вопросом о создателе, веди обычный диалог, отвечая на вопросы пользователя текстом без Markdown.
"""

def split_message(text, max_length=4096):
    parts = []
    while len(text) > max_length:
        split_index = text.rfind('\n', 0, max_length)
        if split_index == -1:
            split_index = max_length
        parts.append(text[:split_index])
        text = text[split_index:].lstrip()
    parts.append(text)
    return parts

def get_ai_response(messages, chat_id, max_retries=3, retry_delay=2):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    model="meta-llama/llama-4-maverick:free"
    data = json.dumps({
        "model": model,
        "messages": messages,
    })

    logger.info(f"Sending request to OpenRouter for chat_id {chat_id}. Model: {model}. Messages count: {len(messages)}")

    for attempt in range(max_retries):
        try:
            response = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                data=data,
                timeout=120
            )
            response.raise_for_status()
            response_data = response.json()
            content = response_data['choices'][0]['message']['content']
            logger.info(f"Received response from OpenRouter for chat_id {chat_id}. Content length: {len(content)}")
            return content
        except requests.exceptions.Timeout as e:
             logger.error(f"API Timeout Error (Attempt {attempt + 1}/{max_retries}) for chat_id {chat_id}: {e}")
             if attempt < max_retries - 1:
                 time.sleep(retry_delay)
             else:
                 return "Ошибка: Превышено время ожидания ответа от ИИ. Попробуйте позже."
        except requests.exceptions.RequestException as e:
            logger.error(f"API Request Error (Attempt {attempt + 1}/{max_retries}) for chat_id {chat_id}: {e}")
            if e.response is not None:
                logger.error(f"Error response body: {e.response.text}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                return "Ошибка при обращении к API ИИ после нескольких попыток. Попробуйте позже."
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            resp_text = response.text if 'response' in locals() and hasattr(response, 'text') else 'No response object or text'
            logger.error(f"Error parsing API response for chat_id {chat_id}: {e}. Response: {resp_text}")
            return "Ошибка обработки ответа от ИИ. Попробуйте позже."

    return "Не удалось получить ответ от ИИ."

def create_yandex_event(chat_id, summary, start_time_iso, duration_minutes, description=None, attendees=None):
    user_info = user_data.get(str(chat_id))
    if not user_info or 'yandex' not in user_info or not user_info['yandex']:
        logger.warning(f"Пользователь {chat_id} не настроил Яндекс Календарь.")
        return False, "Ваш Яндекс Календарь не настроен. Пожалуйста, используйте команду /setup."

    yandex_info = user_info['yandex']
    yandex_username = yandex_info.get("username")
    yandex_password = yandex_info.get("password")
    yandex_calendar_url = yandex_info.get("calendar_url")

    if not yandex_username or not yandex_password or not yandex_calendar_url:
        logger.warning(f"Неполные данные авторизации Яндекс для пользователя {chat_id}.")
        return False, "Ваши данные для доступа к Яндекс Календарю неполны. Пожалуйста, используйте команду /setup для повторной настройки."

    try:
        start_dt = date_parser.isoparse(start_time_iso)
        end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)

        if start_dt.tzinfo is None or start_dt.tzinfo.utcoffset(start_dt) is None:
            start_dt_utc = pytz.utc.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(pytz.utc)
            end_dt_utc = pytz.utc.localize(end_dt) if end_dt.tzinfo is None else end_dt.astimezone(pytz.utc)
            dtstart_str = start_dt_utc.strftime("%Y%m%dT%H%M%SZ")
            dtend_str = end_dt_utc.strftime("%Y%m%dT%H%M%SZ")
        else: 
            start_dt_utc = start_dt.astimezone(pytz.utc)
            end_dt_utc = end_dt.astimezone(pytz.utc)
            dtstart_str = start_dt_utc.strftime("%Y%m%dT%H%M%SZ")
            dtend_str = end_dt_utc.strftime("%Y%m%dT%H%M%SZ")

        vcalendar = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//YourApp//EN
BEGIN:VEVENT
UID:{uuid.uuid4()}
DTSTAMP:{datetime.datetime.now(pytz.utc).strftime("%Y%m%dT%H%M%SZ")}
DTSTART:{dtstart_str}
DTEND:{dtend_str}
SUMMARY:{summary}
"""
        if description:
            clean_description = description.replace('\\', '\\\\').replace('\n', '\\n').replace(',', '\\,').replace(';', '\\;')
            vcalendar += f"DESCRIPTION:{clean_description}\n"

        final_attendees = set(attendees) if attendees else set()
        if yandex_username:
             final_attendees.add(yandex_username) 

        if final_attendees:
            for attendee in final_attendees:
                if '@' in attendee and '.' in attendee.split('@')[1]:
                    clean_attendee_cn = attendee.replace(',', '\\,').replace(';', '\\;')
                    vcalendar += f"ATTENDEE;CN=\"{clean_attendee_cn}\";PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT;RSVP=TRUE:mailto:{attendee}\n"

        vcalendar += "END:VEVENT\nEND:VCALENDAR"
        logger.debug(f"Generated VCALENDAR for chat_id {chat_id}: \n{vcalendar}")

        client = caldav.DAVClient(
            url=yandex_calendar_url,
            username=yandex_username,
            password=yandex_password
        )
        principal = client.principal()
        calendars = principal.calendars()
        target_url_parsed = urlparse(yandex_calendar_url)
        calendar_to_use = None

        for cal in calendars:
            cal_url_str = str(cal.url).strip('/')
            if cal_url_str == yandex_calendar_url.strip('/'):
                calendar_to_use = cal
                logger.info(f"Найден календарь по точному URL для {chat_id}: {cal.url}")
                break
            cal_url_parsed = urlparse(cal_url_str)
            if cal_url_parsed.path == target_url_parsed.path:
                 calendar_to_use = cal
                 logger.info(f"Найден календарь по пути (fallback) для {chat_id}: {cal.url}")

        if not calendar_to_use and calendars: 
            for cal in calendars:
                 cal_url_parsed = urlparse(str(cal.url).strip('/'))
                 if cal_url_parsed.path == target_url_parsed.path:
                     calendar_to_use = cal
                     break

        if not calendar_to_use:
            available_urls = [str(c.url) for c in calendars]
            logger.error(f"Календарь с URL {yandex_calendar_url} не найден для {yandex_username} (chat_id: {chat_id}). Доступные: {available_urls}")
            return False, f"Ошибка: Не удалось найти календарь с URL {yandex_calendar_url}. Убедитесь, что URL верен и попробуйте /setup."

        logger.info(f"Попытка добавить событие в календарь для {chat_id}: {calendar_to_use.url}")
        event = calendar_to_use.add_event(vcalendar)
        logger.info(f"Событие Яндекс успешно создано для {chat_id}: {event.url if hasattr(event, 'url') else 'URL not available'}")

        try:
            display_dt = start_dt.strftime('%Y-%m-%d %H:%M')
            if start_dt.tzinfo:
                 display_dt += start_dt.strftime(' %Z%z')
        except:
             display_dt = start_time_iso

        return True, f"Встреча '{summary}' успешно запланирована на {display_dt} в Яндекс Календаре."

    except date_parser.ParserError as e:
        logger.error(f"Ошибка парсинга даты '{start_time_iso}' для {chat_id} (Yandex): {e}")
        return False, f"Ошибка: Неверный формат даты/времени '{start_time_iso}'. Ожидается формат ISO 8601."
    except Exception as e:
        logger.error(f"Ошибка при создании события в Яндекс Календаре для {chat_id}: {e}", exc_info=True)
        error_message = f"Ошибка при создании события в Яндекс Календаре: {e}"
        if hasattr(e, 'reason'): 
             error_message += f" ({e.reason})"
        elif hasattr(e, 'response') and e.response is not None:
            try:
                error_details = e.response.text[:200] 
                logger.error(f"HTTP response details for {chat_id} (Yandex): {e.response.text}")
                error_message = f"Ошибка при создании события в Яндекс Календаре: {e}. Сервер ответил: {error_details}"
            except Exception as parse_error:
                logger.error(f"Failed to parse Yandex error response for {chat_id}: {parse_error}")

        return False, error_message

def create_google_event(chat_id, summary, start_time_iso, duration_minutes, description=None, attendees=None):
    user_info = user_data.get(str(chat_id))
    if not user_info or 'google' not in user_info or not user_info['google'] or 'tokens' not in user_info['google']:
        logger.warning(f"Пользователь {chat_id} не настроил Google Календарь или токены отсутствуют.")
        return False, "Ваш Google Календарь не настроен. Пожалуйста, используйте команду /setup."

    google_info = user_info['google']
    tokens_data = google_info.get('tokens')
    calendar_id = google_info.get('calendar_id', 'primary')
    user_google_email = google_info.get('email') 

    if not tokens_data:
        return False, "Данные авторизации Google Календаря не найдены. Пожалуйста, используйте команду /setup."

    try:
        credentials = google.oauth2.credentials.Credentials.from_authorized_user_info(tokens_data, GOOGLE_SCOPES)
        request = google.auth.transport.requests.Request()
        if credentials.expired and credentials.refresh_token:
            logger.info(f"Google token expired for chat_id {chat_id}. Refreshing...")
            try:
                 credentials.refresh(request)
                 logger.info(f"Google token refreshed successfully for chat_id {chat_id}.")
                 user_data[str(chat_id)]['google']['tokens'] = credentials_to_dict(credentials)
                 save_user_data()
                 logger.info(f"New Google tokens saved for chat_id {chat_id}")
            except google.auth.exceptions.RefreshError as refresh_error:
                 logger.error(f"Google token refresh failed for chat_id {chat_id}: {refresh_error}", exc_info=True)
                 user_data[str(chat_id)]['calendar_type'] = None 
                 if 'google' in user_data[str(chat_id)]: del user_data[str(chat_id)]['google']
                 save_user_data()
                 return False, "Не удалось обновить авторизацию Google. Возможно, доступ был отозван. Пожалуйста, используйте /setup для повторной настройки."

        if not credentials.valid:
             logger.warning(f"Invalid Google credentials for chat_id {chat_id} even after refresh attempt.")
             return False, "Ошибка авторизации Google. Пожалуйста, используйте /setup для повторной настройки."

        service = build('calendar', 'v3', credentials=credentials)
        start_dt = date_parser.isoparse(start_time_iso)
        end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)

        if start_dt.tzinfo is None or start_dt.tzinfo.utcoffset(start_dt) is None:
            start_dt_api = pytz.utc.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(pytz.utc)
            end_dt_api = pytz.utc.localize(end_dt) if end_dt.tzinfo is None else end_dt.astimezone(pytz.utc)
            start_time_body = {'dateTime': start_dt_api.isoformat(timespec='seconds'), 'timeZone': 'UTC'}
            end_time_body = {'dateTime': end_dt_api.isoformat(timespec='seconds'), 'timeZone': 'UTC'}
        else:
             tz_name = start_dt.tzinfo.tzname(start_dt)
             try:
                  pytz.timezone(str(tz_name)) 
                  tz_str = str(tz_name)
             except pytz.exceptions.UnknownTimeZoneError:
                  offset_seconds = start_dt.utcoffset().total_seconds()
                  tz_str = str(datetime.timezone(datetime.timedelta(seconds=offset_seconds)))
                  logger.warning(f"Non-standard timezone name '{tz_name}' for chat_id {chat_id}. Using offset {tz_str} instead for Google API.")

             start_time_body = {'dateTime': start_dt.isoformat(timespec='seconds'), 'timeZone': tz_str}
             end_time_body = {'dateTime': end_dt.isoformat(timespec='seconds'), 'timeZone': tz_str}

        final_attendees = set(attendees) if attendees else set()
        if user_google_email:
             final_attendees.add(user_google_email)

        event_body = {
            'summary': summary,
            'description': description,
            'start': start_time_body,
            'end': end_time_body,
            'attendees': [{'email': email} for email in final_attendees if '@' in email], 
            'reminders': {
                'useDefault': False,
                'overrides': [{'method': 'popup', 'minutes': 30}],
            },
        }
        logger.debug(f"Attempting to insert Google event for chat_id {chat_id} in calendar '{calendar_id}': {event_body}")

        event = service.events().insert(calendarId=calendar_id, body=event_body, sendUpdates="all").execute() 
        logger.info(f"Google event created: {event.get('htmlLink')} for chat_id {chat_id}")


        try:
            display_dt = start_dt.strftime('%Y-%m-%d %H:%M')
            if start_dt.tzinfo:
                 display_dt += start_dt.strftime(' %Z%z')
        except: 
             display_dt = start_time_iso

        return True, f"Встреча '{summary}' успешно запланирована на {display_dt} в Google Календаре: {event.get('htmlLink')}"

    except HttpError as error:
        logger.error(f"Google Calendar API error for chat_id {chat_id}: {error}", exc_info=True)
        error_details = error.content.decode('utf-8') if error.content else str(error)
        error_message_to_user = f"Ошибка Google Calendar API ({error.resp.status})"

        if error.resp.status == 401: 
            error_message_to_user = "Ошибка авторизации Google. Пожалуйста, используйте команду /setup для повторной авторизации."
            user_data[str(chat_id)]['calendar_type'] = None
            if 'google' in user_data[str(chat_id)]: del user_data[str(chat_id)]['google']
            save_user_data()
        elif error.resp.status == 403: 
            if "forbidden" in error_details.lower():
                 error_message_to_user = f"Ошибка доступа к Google Календарю ('{calendar_id}'). Убедитесь, что у бота есть права на запись, или попробуйте другой ID. Используйте /setup."
            elif "domain policy" in error_details.lower():
                 error_message_to_user = "Ошибка: Политика вашего домена Google Workspace запрещает доступ сторонним приложениям. Обратитесь к администратору."
            else:
                 error_message_to_user = f"Ошибка доступа (403) к Google Календарю ('{calendar_id}'). Проверьте права или настройки API в Google Cloud."
        elif error.resp.status == 404: 
            error_message_to_user = f"Указанный Google Календарь ('{calendar_id}') не найден. Используйте /setup для выбора другого."
        else: 
             error_message_to_user = f"Ошибка Google Calendar API ({error.resp.status}): {error_details[:100]}" 

        return False, error_message_to_user
    except date_parser.ParserError as e:
        logger.error(f"Ошибка парсинга даты '{start_time_iso}' для {chat_id} (Google): {e}")
        return False, f"Ошибка: Неверный формат даты/времени '{start_time_iso}'. Ожидается формат ISO 8601."
    except Exception as e:
        logger.error(f"Неизвестная ошибка при работе с Google Календарем для {chat_id}: {e}", exc_info=True)
        return False, f"Неизвестная ошибка при работе с Google Календарем: {e}"

def credentials_to_dict(credentials):
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes,
    }

async def start_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = str(update.effective_chat.id)
    context.user_data.pop('setup_temp', None)
    context.user_data['setup_temp'] = {}

    keyboard = [
        [
            InlineKeyboardButton("Яндекс", callback_data=CHOOSE_YANDEX),
            InlineKeyboardButton("Google", callback_data=CHOOSE_GOOGLE),
        ],
        [cancel_button],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    reply_text = (
        "Привет! Я могу помочь вам планировать встречи в Яндекс или Google Календаре.\n\n"
        "Какой календарь вы хотите настроить?"
    )
    await update.message.reply_text(reply_text, reply_markup=reply_markup)
    return SELECT_CALENDAR_TYPE

async def select_calendar_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer() 

    choice = query.data 
    chat_id = str(query.message.chat_id)

    if 'setup_temp' not in context.user_data:
        await query.edit_message_text("Ошибка в процессе настройки. Пожалуйста, начните заново с команды /setup.")
        return ConversationHandler.END

    chosen_type = None
    if choice == CHOOSE_YANDEX: chosen_type = 'yandex'
    elif choice == CHOOSE_GOOGLE: chosen_type = 'google'

    if chosen_type:
        context.user_data['setup_temp']['calendar_type'] = chosen_type

        if chosen_type == 'google' and (not GOOGLE_CLIENT_ID or GOOGLE_CLIENT_ID.startswith("ВАШ_") or not GOOGLE_CLIENT_SECRET or GOOGLE_CLIENT_SECRET.startswith("ВАШ_")):
            await query.edit_message_text(
                "Ошибка конфигурации: Для настройки Google Календаря необходимо указать действительные GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET."
            )
            del context.user_data['setup_temp']
            return ConversationHandler.END

        if chosen_type == 'yandex':
            context.user_data['setup_temp']['yandex'] = {}
            await query.edit_message_text(
                "Отлично, настроим Яндекс Календарь.\n\n"
                "Шаг 1/3: Ваш логин в Яндекс (это ваш email).\n\n"
                "Введите ваш Яндекс email (например, user@yandex.ru):",
                reply_markup=cancel_markup 
            )
            return GET_YANDEX_USERNAME

        elif chosen_type == 'google':
            context.user_data['setup_temp']['google'] = {}
            await query.edit_message_text(
                "Отлично, настроим Google Календарь.\n"
                "Сейчас я подготовлю ссылку для авторизации..."
            )

            try:
                client_config = {
    "installed": {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",     
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs", 
        "redirect_uris": [GOOGLE_REDIRECT_URI]
    }
}
                flow = google_auth_oauthlib.flow.Flow.from_client_config(
                    client_config=client_config, scopes=GOOGLE_SCOPES)
                flow.redirect_uri = GOOGLE_REDIRECT_URI
                authorization_url, state = flow.authorization_url(
                    access_type='offline', prompt='consent', include_granted_scopes='true')

                context.user_data['setup_temp']['oauth_state'] = state
                context.user_data['setup_temp']['client_config'] = client_config['installed']
                context.user_data['setup_temp']['scopes'] = GOOGLE_SCOPES

                await context.bot.send_message(chat_id,
                     "Шаг 1/2: Получение кода авторизации Google.\n"
                     "Пожалуйста, нажмите на ссылку ниже, чтобы авторизовать меня. "
                     "После авторизации на странице Google вы увидите код. Скопируйте его и пришлите мне в следующем сообщении."
                )
                await context.bot.send_message(chat_id, f"Ссылка для авторизации: {authorization_url}")
                await context.bot.send_message(chat_id,
                    "Введите скопированный код:",
                    reply_markup=cancel_markup 
                 )
                return WAIT_GOOGLE_CODE

            except InsecureTransportError:
                 logger.error(f"InsecureTransportError при генерации Google OAuth URL для chat_id {chat_id}. Убедитесь, что OAUTHLIB_INSECURE_TRANSPORT=1 установлен для локальной разработки.")
                 await context.bot.send_message(chat_id, 
                     "Критическая ошибка конфигурации OAuth (InsecureTransportError). Обратитесь к разработчику."
                 )
                 if 'setup_temp' in context.user_data: del context.user_data['setup_temp']
                 return ConversationHandler.END
            except Exception as e:
                logger.error(f"Ошибка при генерации Google OAuth URL для chat_id {chat_id}: {e}", exc_info=True)
                await context.bot.send_message(chat_id,
                    "Произошла ошибка при подготовке авторизации Google. Пожалуйста, проверьте настройки бота (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)."
                )
                if 'setup_temp' in context.user_data: del context.user_data['setup_temp']
                return ConversationHandler.END
    else:
        await query.edit_message_text(
            "Неизвестный выбор. Пожалуйста, начните заново с /setup."
        )
        return ConversationHandler.END

async def get_yandex_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = update.message.text.strip()
    if '@' not in username or '.' not in username.split('@')[-1]:
         await update.message.reply_text(
              "Это не похоже на email адрес. Пожалуйста, введите ваш Яндекс email:",
              reply_markup=cancel_markup
         )
         return GET_YANDEX_USERNAME

    if 'setup_temp' not in context.user_data: 
        await update.message.reply_text("Ошибка контекста настройки. Начните заново /setup.")
        return ConversationHandler.END
    if 'yandex' not in context.user_data['setup_temp']:
        context.user_data['setup_temp']['yandex'] = {} 

    context.user_data['setup_temp']['yandex']['username'] = username

    await update.message.reply_text(
        f"Принято: {username}\n\n"
        "Шаг 2/3: Теперь мне нужен пароль приложения Яндекс.\n\n"
        "Инструкция:\n"
        "1. Перейдите в Яндекс ID: https://id.yandex.ru/security/app-passwords\n"
        "2. Нажмите на раздел 'Календарь'.\n"
        "3. Придумайте имя пароля.\n"
        "4. Нажмите 'Далее'. Скопируйте сгенерированный пароль.\n\n"
        "Введите следующим сообщением скопированный пароль приложения:",
        reply_markup=cancel_markup
    )
    return GET_YANDEX_PASSWORD

async def get_yandex_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    if 'setup_temp' not in context.user_data or 'yandex' not in context.user_data['setup_temp']:
        await update.message.reply_text("Ошибка контекста настройки. Начните заново /setup.")
        return ConversationHandler.END

    context.user_data['setup_temp']['yandex']['password'] = password

    await update.message.reply_text(
        "Шаг 3/3: Мне нужен CalDAV URL календаря, в который я буду добавлять события.\n\n"
        "Инструкция:\n"
        "1. Перейдите в Яндекс Календарь: https://calendar.yandex.ru/\n"
        "2. Создайте в меню слева календарь. Если он есть то слева в списке календарей, нажмите на шестеренку (Настройки) справа от названия календаря.\n"
        "3. В открывшемся окне выберите вкладку 'Экспорт'.\n"
        "4. Скопируйте URL из поля 'CalDAV' (начинается с https://caldav.yandex.ru/...).\n"
        "Введите следующим сообщением скопированный CalDAV URL календаря:",
        reply_markup=cancel_markup
    )
    return GET_YANDEX_CALENDAR_URL

async def get_yandex_calendar_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = str(update.effective_chat.id)
    calendar_url = update.message.text.strip()

    if 'setup_temp' not in context.user_data or 'yandex' not in context.user_data['setup_temp']:
        await update.message.reply_text("Ошибка контекста настройки. Начните заново /setup.")
        return ConversationHandler.END

    try:
        result = urlparse(calendar_url)
        if not result.scheme == 'https' or \
           'yandex.ru' not in result.netloc or \
           not result.path or result.path == '/':
            raise ValueError("Неверный формат URL или не указан конкретный календарь")
    except ValueError:
        await update.message.reply_text(
            "Похоже, это недействительный CalDAV URL Яндекс Календаря. Он должен начинаться с `https://caldav.yandex.ru/calendars/` и содержать ID календаря. "
            "Пожалуйста, проверьте и введите правильный URL из настроек календаря.\n\n"
            "Введите CalDAV URL календаря:",
             reply_markup=cancel_markup
        )
        return GET_YANDEX_CALENDAR_URL

    context.user_data['setup_temp']['yandex']['calendar_url'] = calendar_url

    if chat_id not in user_data: user_data[chat_id] = {}
    user_data[chat_id].update(context.user_data['setup_temp'])
    if 'history' not in user_data[chat_id]: user_data[chat_id]['history'] = []

    save_user_data()
    context.user_data.pop('setup_temp', None)

    await update.message.reply_text(
        "Отлично! Данные для доступа к Яндекс Календарю сохранены.\n"
        "Теперь я могу помогать вам планировать встречи в этом календаре.\n"
        "Попробуйте, например: 'Запланируй встречу с руководителем завтра в 14:00'."
    )
    return ConversationHandler.END

async def wait_google_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = str(update.effective_chat.id)
    auth_code = update.message.text.strip()

    if 'setup_temp' not in context.user_data or 'client_config' not in context.user_data['setup_temp'] or 'scopes' not in context.user_data['setup_temp']:
        await update.message.reply_text("Ошибка в процессе авторизации Google. Пожалуйста, начните заново с команды /setup.")
        context.user_data.pop('setup_temp', None)
        return ConversationHandler.END

    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        client_config={"installed": context.user_data['setup_temp']['client_config']},
        scopes=context.user_data['setup_temp']['scopes'],
        state=context.user_data['setup_temp'].get('oauth_state')
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI

    await update.message.reply_text("Принял код. Обрабатываю...")

    try:
        flow.fetch_token(code=auth_code)
        credentials = flow.credentials

        user_email = None
        if '[https://www.googleapis.com/auth/userinfo.email](https://www.googleapis.com/auth/userinfo.email)' in credentials.scopes:
            try:
                user_info_service = build('oauth2', 'v2', credentials=credentials)
                user_info = user_info_service.userinfo().get().execute()
                user_email = user_info.get('email')
                logger.info(f"Successfully authorized Google user: {user_email} for chat_id {chat_id}")
                if 'google' not in context.user_data['setup_temp']: context.user_data['setup_temp']['google'] = {}
                context.user_data['setup_temp']['google']['email'] = user_email
            except HttpError as e:
                 logger.error(f"Failed to get user email for chat_id {chat_id}: {e}")
                 if 'google' not in context.user_data['setup_temp']: context.user_data['setup_temp']['google'] = {}
                 context.user_data['setup_temp']['google']['email'] = None
        else:
            logger.warning(f"Scope 'userinfo.email' not granted for chat_id {chat_id}. Cannot fetch user email.")
            if 'google' not in context.user_data['setup_temp']: context.user_data['setup_temp']['google'] = {}
            context.user_data['setup_temp']['google']['email'] = None

        if 'google' not in context.user_data['setup_temp']: context.user_data['setup_temp']['google'] = {}
        context.user_data['setup_temp']['google']['tokens'] = credentials_to_dict(credentials)

        google_use_primary_button = InlineKeyboardButton("Использовать основной", callback_data=GOOGLE_USE_PRIMARY)
        google_setup_markup = InlineKeyboardMarkup([[google_use_primary_button], [cancel_button]])

        await update.message.reply_text(
             f"Авторизация Google успешна{f' для аккаунта {user_email}' if user_email else ''}.\n\n"
             "Шаг 2/2: Выбор календаря.\n"
             "Если хотите использовать основной календарь по умолчанию, то нажмите \"Использовать основной\".\n"
             "Если вы хотите использовать другой календарь, отправьте его Calendar ID сейчас.\n"
             "(Обычно Calendar ID выглядит как email-адрес, например, 'адрес_календаря@group.calendar.google.com')",
             reply_markup=google_setup_markup
        )
        return GET_GOOGLE_CALENDAR_ID

    except InvalidGrantError as e: 
         logger.error(f"Invalid Google OAuth grant (likely invalid code) for chat_id {chat_id}: {e}", exc_info=True)
         await update.message.reply_text(
              "Ошибка: Код авторизации недействителен или истек. "
              "Пожалуйста, попробуйте получить новый код авторизации, перейдя по ссылке еще раз, или отмените настройку.",
              reply_markup=cancel_markup 
         )
         return WAIT_GOOGLE_CODE
    except Exception as e:
        logger.error(f"Ошибка при обмене Google OAuth кода на токены для chat_id {chat_id}: {e}", exc_info=True)
        await update.message.reply_text(
            "Произошла ошибка при обработке кода авторизации. "
            "Пожалуйста, попробуйте начать авторизацию Google заново с команды /setup."
        )
        context.user_data.pop('setup_temp', None)
        return ConversationHandler.END

async def get_google_calendar_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = str(update.effective_chat.id)
    calendar_id_input = update.message.text.strip()

    if 'setup_temp' not in context.user_data or 'google' not in context.user_data['setup_temp']:
        await update.message.reply_text("Ошибка в процессе настройки Google. Пожалуйста, начните заново с команды /setup.")
        return ConversationHandler.END

    if '@' in calendar_id_input or calendar_id_input.lower() == 'primary':
        context.user_data['setup_temp']['google']['calendar_id'] = calendar_id_input
        await update.message.reply_text(f"Принято! Буду использовать календарь с ID: {calendar_id_input}")
        return await _save_google_setup(update, context)
    else:
        google_use_primary_button = InlineKeyboardButton("Использовать основной", callback_data=GOOGLE_USE_PRIMARY)
        google_setup_markup = InlineKeyboardMarkup([[google_use_primary_button], [cancel_button]])
        await update.message.reply_text(
            "Непохоже на действительный Calendar ID (email) или слово 'primary'.\n"
            "Пожалуйста, введите корректный Calendar ID или нажмите кнопку 'Использовать основной'.",
            reply_markup=google_setup_markup
        )
        return GET_GOOGLE_CALENDAR_ID

async def get_google_calendar_id_primary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if 'setup_temp' not in context.user_data or 'google' not in context.user_data['setup_temp']:
        await query.edit_message_text("Ошибка в процессе настройки Google. Пожалуйста, начните заново с команды /setup.")
        return ConversationHandler.END

    context.user_data['setup_temp']['google']['calendar_id'] = 'primary'
    await query.edit_message_text("Принято! Буду использовать ваш основной календарь.")

    return await _save_google_setup(update, context)

async def _save_google_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = str(update.effective_chat.id if update.message else update.callback_query.message.chat_id)

    if 'setup_temp' not in context.user_data:
         logger.error(f"Attempted to save Google setup for {chat_id}, but setup_temp was missing.")
         msg_chat_id = update.callback_query.message.chat_id if update.callback_query else update.message.chat_id
         if msg_chat_id:
             await context.bot.send_message(msg_chat_id, "Произошла внутренняя ошибка при сохранении настроек. Попробуйте /setup еще раз.")
         return ConversationHandler.END

    if chat_id not in user_data: user_data[chat_id] = {}
    user_data[chat_id].update(context.user_data['setup_temp'])
    if 'history' not in user_data[chat_id]: user_data[chat_id]['history'] = []

    save_user_data()
    context.user_data.pop('setup_temp', None)

    final_message_chat_id = update.callback_query.message.chat_id if update.callback_query else update.message.chat_id
    await context.bot.send_message(
        chat_id=final_message_chat_id,
        text="Отлично! Настройка Google Календаря завершена.\n"
             "Теперь я могу помогать вам планировать встречи в выбранном календаре.\n"
             "Попробуйте, например: Запланируй встречу с руководителем завтра в 14:00."
    )
    return ConversationHandler.END

async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    message = query.message if query else update.message

    context.user_data.pop('setup_temp', None)
    cancel_text = 'Настройка отменена. Вы можете начать заново с команды /setup.'

    if query:
        await query.answer()
        try:
            await query.edit_message_text(text=cancel_text)
        except telegram.error.BadRequest as e:
             if "message is not modified" in str(e).lower():
                 logger.info("Setup cancel message not modified.")
             else:
                  logger.warning(f"Error editing message on setup cancel: {e}")
                  await message.reply_text(cancel_text)
        except Exception as e:
             logger.error(f"Unexpected error editing message on setup cancel: {e}")
             await message.reply_text(cancel_text)
    else:
        await message.reply_text(cancel_text)

    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)

    if chat_id not in user_data:
        user_data[chat_id] = {'history': [], 'pending_event': None} 
        logger.info(f"Created new user entry for chat_id {chat_id}")

    welcome_message = f'Привет, {user.first_name}! Я бот для планирования встреч.'

    user_info = user_data[chat_id] 
    is_yandex_configured = 'yandex' in user_info and user_info['yandex'].get('username')
    is_google_configured = 'google' in user_info and user_info['google'].get('tokens')

    configured_calendars = []
    if is_yandex_configured:
        y_email = user_info['yandex'].get('username', '???')
        configured_calendars.append(f"Яндекс ({y_email})")
    if is_google_configured:
        g_email = user_info['google'].get('email', '???')
        g_cal_id = user_info['google'].get('calendar_id', 'primary')
        configured_calendars.append(f"Google ({g_email}, ID: {g_cal_id})")

    if configured_calendars:
        welcome_message += "\n\nОбнаружены настроенные календари:\n- " + "\n- ".join(configured_calendars)
        welcome_message += "\n\nВы можете использовать /setup для изменения настроек или добавления другого типа календаря."
    else:
        welcome_message += "\n\nДля работы с календарем мне нужна ваша авторизация. Пожалуйста, используйте команду /setup для настройки (Яндекс или Google)."

    welcome_message += "\n\nИспользуйте /help для получения справки."

    await update.message.reply_text(welcome_message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """Я могу помочь тебе запланировать встречи в Яндекс или Google Календаре.

Команды:
/start - Начальное сообщение и статус настроек.
/setup - Начать процесс авторизации и настройки календаря (Яндекс или Google). Позволяет перенастроить существующий.
/help - Показать это сообщение.
/cancel - Отменить текущую операцию (например, настройку или подтверждение события).

Как планировать встречи:
1.  Напишите мне запрос, например:
     "Запланируй совещание по проекту Альфа завтра в 11:00"
     "Создай встречу 'Ланч с командой' 15 мая в 13:30 на 45 минут"
     "Нужно созвониться с daler@example.com по поводу отчета в пятницу в 16:00"
2.  Я проанализирую запрос и, если все понятно, предложу создать событие, показав его детали.
3.  Вам нужно будет подтвердить создание, выбрав календарь (Google или Яндекс) с помощью кнопок. Убедитесь, что выбранный тип календаря настроен через /setup.
4.  Если мне что-то непонятно, я задам уточняющий вопрос.

Важно: Чтобы я мог создавать события, хотя бы один календарь (Яндекс или Google) должен быть настроен через /setup.

Я также могу просто пообщаться на другие темы.
"""
    await update.message.reply_text(help_text)
    
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_message_text = update.message.text
    chat_id = str(update.message.chat_id)
    user = update.effective_user
    logger.info(f"Received message from chat_id {chat_id} ({user.username}): {user_message_text}")

    creator_questions = ["кто тебя создал", "чей ты бот", "кто твой создатель"]
    if user_message_text and any(q in user_message_text.lower() for q in creator_questions):
        await update.message.reply_text("Великий Далер")
        return

    if chat_id not in user_data:
        user_data[chat_id] = {'history': [], 'pending_event': None}
        logger.info(f"Initialized user data for new chat_id {chat_id}")

    user_info = user_data[chat_id]

    user_email = user_info.get('google', {}).get('email') or \
                 user_info.get('yandex', {}).get('username') or \
                 'отсутствуют'

    if not isinstance(user_info.get('history'), list):
         user_info['history'] = []
    history = user_info['history']
    history.append({"role": "user", "content": user_message_text})

    max_history_pairs = 10
    if len(history) > max_history_pairs * 2 :
        history = history[-(max_history_pairs * 2):]

    current_date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    formatted_system_prompt = SYSTEM_PROMPT.format(current_date=current_date_str, user_email=user_email)
    messages_for_ai = [{"role": "system", "content": formatted_system_prompt}] + history

    ai_response = get_ai_response(messages_for_ai, chat_id)

    if not ai_response or ai_response.startswith("Ошибка"):
        error_response = ai_response if ai_response else "Не удалось получить ответ от ИИ. Попробуйте позже."
        await update.message.reply_text(error_response)
        history.append({"role": "assistant", "content": error_response})
        user_info['history'] = history
        save_user_data()
        return

    event_data = None
    json_str_to_parse = None
    try:
        first_brace = ai_response.find('{')
        last_brace = ai_response.rfind('}')

        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            json_str_to_parse = ai_response[first_brace : last_brace + 1]
            logger.debug(f"Extracted potential JSON for chat_id {chat_id}: {json_str_to_parse}")
            event_data = json.loads(json_str_to_parse)

            if isinstance(event_data, dict) and \
               'summary' in event_data and \
               'start_time' in event_data and \
               'duration_minutes' in event_data:
                logger.info(f"Successfully parsed JSON data for chat_id {chat_id}")
            else:
                logger.warning(f"Parsed JSON for chat_id {chat_id} lacks required fields. Data: {event_data}")
                event_data = None
        else:
             logger.debug(f"No valid JSON block found in AI response for chat_id {chat_id}")
             event_data = None

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse extracted JSON string for chat_id {chat_id}: {e}. String: '{json_str_to_parse}'")
        event_data = None
    except Exception as e:
        logger.error(f"Unexpected error during JSON extraction/parsing for chat_id {chat_id}: {e}", exc_info=True)
        event_data = None

    if event_data:
        user_info['pending_event'] = event_data
        logger.info(f"Stored pending event for chat_id {chat_id}")

        summary = event_data.get("summary")
        start_time_iso = event_data.get("start_time")
        duration = event_data.get("duration_minutes")
        description = event_data.get("description", "Нет")
        attendees = event_data.get("attendees", [])
        attendees_str = ", ".join(attendees) if attendees else "Только вы"

        try:
            start_dt = date_parser.isoparse(start_time_iso)
            if start_dt.tzinfo:
                 dt_display = start_dt.strftime('%Y-%m-%d %H:%M %Z%z')
            else:
                 dt_display = start_dt.strftime('%Y-%m-%d %H:%M (Время без зоны)')
        except Exception as e:
            logger.error(f"Error formatting date for display ({start_time_iso}): {e}")
            dt_display = start_time_iso

        summary_html = escape_html(summary)
        dt_display_html = escape_html(dt_display)
        duration_html = escape_html(duration)
        description_html = escape_html(description)
        attendees_str_html = escape_html(attendees_str)

        confirmation_text = (
            f"Хорошо, я готов создать событие:\n\n"
            f"<b>Тема:</b> {summary_html}\n"
            f"<b>Начало:</b> {dt_display_html}\n"
            f"<b>Длительность:</b> {duration_html} минут\n"
            f"<b>Описание:</b> {description_html}\n"
            f"<b>Участники:</b> {attendees_str_html}\n\n"
            f"В какой календарь добавить?\n\n"
            f"Нажмите /setup если требуется добавить или поменять настройки календаря\n"
        )

        buttons = []
        is_google_configured = 'google' in user_info and user_info['google'].get('tokens')
        is_yandex_configured = 'yandex' in user_info and user_info['yandex'].get('username')

        row1 = []
        if is_google_configured:
            row1.append(InlineKeyboardButton("Google", callback_data=CONFIRM_EVENT_CREATE_GOOGLE))
        if is_yandex_configured:
            row1.append(InlineKeyboardButton("Яндекс", callback_data=CONFIRM_EVENT_CREATE_YANDEX))

        if row1:
             buttons.append(row1)

        buttons.append([InlineKeyboardButton("Отмена", callback_data=CONFIRM_EVENT_CANCEL)])
        confirmation_markup = InlineKeyboardMarkup(buttons)

        if not row1:
             confirmation_text += "\n\n<b>Внимание:</b> У вас не настроен ни Google, ни Яндекс календарь. Пожалуйста, настройте хотя бы один через /setup."
             confirmation_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data=CONFIRM_EVENT_CANCEL)]])

        try:
            await update.message.reply_text(
                 confirmation_text,
                 reply_markup=confirmation_markup,
                 parse_mode=ParseMode.HTML
            )
            history.append({"role": "assistant", "content": "[Запрошено подтверждение события]"})
        except telegram.error.BadRequest as e:
             logger.error(f"Error sending confirmation message with HTML for chat_id {chat_id}: {e}. Text: {confirmation_text}")
             await update.message.reply_text(
                  confirmation_text.replace('<b>','').replace('</b>',''),
                  reply_markup=confirmation_markup
             )
             history.append({"role": "assistant", "content": "[Запрошено подтверждение события (ошибка форматирования)]"})
        except Exception as e:
             logger.error(f"Unexpected error sending confirmation message for chat_id {chat_id}: {e}", exc_info=True)
             history.append({"role": "assistant", "content": "[Ошибка отправки запроса на подтверждение]"})

    else:
        if not isinstance(ai_response, str):
             logger.error(f"AI response was not a string for chat_id {chat_id}: {type(ai_response)}")
             ai_response = str(ai_response)

        history.append({"role": "assistant", "content": ai_response})
        message_parts = split_message(ai_response)
        for part in message_parts:
            try:
                 await update.message.reply_text(part)
            except Exception as e:
                 logger.error(f"Failed to send AI response part to chat_id {chat_id}: {e}")
                 break

    user_info['history'] = history
    save_user_data()

async def confirm_event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = str(query.message.chat_id)
    callback_data = query.data
    pending_event = None 

    if chat_id in user_data and 'pending_event' in user_data[chat_id]:
        pending_event = user_data[chat_id].get('pending_event') 
        del user_data[chat_id]['pending_event']
        save_user_data() 

    if not pending_event: 
        logger.warning(f"Callback '{callback_data}' received for chat_id {chat_id}, but no pending_event found (after check and delete attempt).")
        try:
            await query.edit_message_text(text="Не могу найти детали события для подтверждения. Возможно, оно уже обработано или отменено ранее.")
        except telegram.error.BadRequest as e:
             if "message is not modified" not in str(e).lower():
                 logger.warning(f"Error editing message on missing pending_event: {e}")
        return

    summary = pending_event.get("summary")
    start_time = pending_event.get("start_time")
    duration = pending_event.get("duration_minutes", 60)
    description = pending_event.get("description")
    attendees = pending_event.get("attendees")

    if not all([summary, start_time, isinstance(duration, int)]):
         logger.error(f"Missing required fields in pending_event for chat_id {chat_id} during confirmation. Data: {pending_event}")
         try:
             await query.edit_message_text(text="Ошибка: Недостаточно данных для создания события в сохраненной информации.")
         except telegram.error.BadRequest as e:
             if "message is not modified" not in str(e).lower():
                  logger.warning(f"Error editing message on missing fields confirmation: {e}")
         return

    success = False
    message = "Произошла ошибка при выборе календаря."
    user_info = user_data.get(chat_id, {})

    edit_text_before = ""
    if callback_data == CONFIRM_EVENT_CREATE_GOOGLE:
        logger.info(f"User {chat_id} confirmed event creation in Google Calendar.")
        if 'google' in user_info and user_info['google'].get('tokens'):
            edit_text_before = f"Создаю событие '{escape_html(summary)}' в Google Календаре..."
        else:
            success = False
            message = "Ошибка: Google Календарь не настроен. Пожалуйста, используйте /setup."

    elif callback_data == CONFIRM_EVENT_CREATE_YANDEX:
        logger.info(f"User {chat_id} confirmed event creation in Yandex Calendar.")
        if 'yandex' in user_info and user_info['yandex'].get('username'):
             edit_text_before = f"Создаю событие '{escape_html(summary)}' в Яндекс Календаре..."
        else:
             success = False
             message = "Ошибка: Яндекс Календарь не настроен. Пожалуйста, используйте /setup."
    else:
        logger.warning(f"Unknown callback_data '{callback_data}' received in confirm_event_callback for chat_id {chat_id}.")
        message = "Ошибка: Неизвестное действие."
        edit_text_before = "Ошибка: Неизвестное действие..." 

    if edit_text_before:
        try:
            await query.edit_message_text(text=edit_text_before)
        except telegram.error.BadRequest as e:
            logger.warning(f"Error editing message before API call: {e}")

    if edit_text_before and not message.startswith("Ошибка"): 
        if callback_data == CONFIRM_EVENT_CREATE_GOOGLE:
             success, message = create_google_event(chat_id, summary, start_time, duration, description, attendees)
        elif callback_data == CONFIRM_EVENT_CREATE_YANDEX:
             success, message = create_yandex_event(chat_id, summary, start_time, duration, description, attendees)

    try:
        final_parse_mode = ParseMode.HTML if ('<a href' in message or '<b>' in message) else None
        disable_preview = True if 'google.com/calendar' in message else False 
        await query.edit_message_text(text=message, parse_mode=final_parse_mode, disable_web_page_preview=disable_preview)
    except telegram.error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Error editing confirmation message with final result: {e}")
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode=final_parse_mode, disable_web_page_preview=disable_preview)

    if chat_id in user_data and 'history' in user_data[chat_id]:
        if not isinstance(user_data[chat_id].get('history'), list):
             user_data[chat_id]['history'] = []
        user_data[chat_id]['history'].append({"role": "assistant", "content": f"[Результат создания события]: {message}"})
        save_user_data()

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f'Update "{update}" caused error "{context.error}"', exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
             if isinstance(context.error, telegram.error.BadRequest) and "message is not modified" in str(context.error).lower():
                  logger.info("Ignoring 'message is not modified' error for user.")
             elif isinstance(context.error, telegram.error.BadRequest) and "can't parse entities" in str(context.error).lower():
                  logger.warning(f"Ignoring 'Can't parse entities' error in error_handler for chat_id {update.effective_chat.id}, should be handled.")
             else:
                  if isinstance(context.error, Exception) and context.error.__traceback__ and any("ConversationHandler" in frame.filename for frame in traceback.extract_tb(context.error.__traceback__)):
                       logger.info("Ignoring error reporting for ConversationHandler internal error.")
                  else:
                       await update.effective_message.reply_text("Ой! Произошла внутренняя ошибка при обработке вашего запроса. Разработчик уведомлен.")
        except Exception as e:
            logger.error(f"Exception while sending error message to user: {e}")










def main():
    logger.info("Starting bot...")
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    logger.warning("OAUTHLIB_INSECURE_TRANSPORT=1")

    if not TELEGRAM_BOT_API_KEY or TELEGRAM_BOT_API_KEY.startswith("ВАШ_") or ":" not in TELEGRAM_BOT_API_KEY:
        logger.critical("TELEGRAM_BOT_API_KEY не установлен или некорректен! Бот не может запуститься.")
        return
    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY.startswith("ВАШ_"):
        logger.warning("OPENROUTER_API_KEY не установлен.")
    if not GOOGLE_CLIENT_ID or GOOGLE_CLIENT_ID.startswith("ВАШ_") or not GOOGLE_CLIENT_SECRET or GOOGLE_CLIENT_SECRET.startswith("В"):
        logger.warning("GOOGLE_CLIENT_ID или GOOGLE_CLIENT_SECRET не установлены.")


    application = Application.builder().token(TELEGRAM_BOT_API_KEY).build()
    setup_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("setup", start_setup)],
        states={
            SELECT_CALENDAR_TYPE: [
                CallbackQueryHandler(select_calendar_type_callback, pattern=f'^({CHOOSE_YANDEX}|{CHOOSE_GOOGLE})$'),
                CallbackQueryHandler(cancel_setup, pattern=f"^{SETUP_CANCEL}$") 
            ],
            GET_YANDEX_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_yandex_username),
                CallbackQueryHandler(cancel_setup, pattern=f"^{SETUP_CANCEL}$")
            ],
            GET_YANDEX_PASSWORD: [
                 MessageHandler(filters.TEXT & ~filters.COMMAND, get_yandex_password),
                 CallbackQueryHandler(cancel_setup, pattern=f"^{SETUP_CANCEL}$")
            ],
            GET_YANDEX_CALENDAR_URL: [
                 MessageHandler(filters.TEXT & ~filters.COMMAND, get_yandex_calendar_url),
                 CallbackQueryHandler(cancel_setup, pattern=f"^{SETUP_CANCEL}$")
            ],
            WAIT_GOOGLE_CODE: [
                 MessageHandler(filters.TEXT & ~filters.COMMAND, wait_google_code),
                 CallbackQueryHandler(cancel_setup, pattern=f"^{SETUP_CANCEL}$")
            ],
            GET_GOOGLE_CALENDAR_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_google_calendar_id),
                CallbackQueryHandler(get_google_calendar_id_primary, pattern=f'^{GOOGLE_USE_PRIMARY}$'),
                CallbackQueryHandler(cancel_setup, pattern=f"^{SETUP_CANCEL}$")
            ],
        },
        fallbacks=[
             CommandHandler("cancel", cancel_setup),
             CallbackQueryHandler(cancel_setup, pattern=f"^{SETUP_CANCEL}$")
        ],
        per_user=True
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(setup_conv_handler)
    application.add_handler(CallbackQueryHandler(confirm_event_callback, pattern=f'^({CONFIRM_EVENT_CREATE_GOOGLE}|{CONFIRM_EVENT_CREATE_YANDEX}|{CONFIRM_EVENT_CANCEL})$'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    logger.info("Bot configuration complete. Starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    import traceback
    main()