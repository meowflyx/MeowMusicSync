"""Actionable setup/API errors shared by the CLI and Telegram bot."""
from requests.exceptions import RequestException
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from yandex_music.exceptions import UnauthorizedError, NetworkError


def explain_error(error):
    if type(error).__module__.startswith("aiogram."):
        from aiogram.exceptions import TelegramUnauthorizedError, TelegramConflictError, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
        if isinstance(error, TelegramUnauthorizedError):
            return "Telegram: токен бота недействителен. Обновите TG_BOT_TOKEN через @BotFather и перезапустите службу."
        if isinstance(error, TelegramConflictError):
            return "Telegram: другой процесс уже получает сообщения этого бота. Остановите вторую копию или проверьте webhook."
        if isinstance(error, TelegramForbiddenError):
            return "Telegram: бот заблокирован или не имеет доступа к чату. Разблокируйте его и отправьте /start."
        if isinstance(error, TelegramRetryAfter):
            return f"Telegram: лимит сообщений. Повторите через {error.retry_after} секунд."
        if isinstance(error, TelegramNetworkError):
            return "Telegram недоступен. Проверьте подключение к сети."
    if isinstance(error, SpotifyOauthError):
        if error.error == "invalid_client":
            return "Spotify: неверная пара Client ID / Client Secret. Скопируйте оба из одного приложения в .env."
        if error.error in ("invalid_grant", "access_denied"):
            return "Spotify: код истёк, уже использован или доступ отклонён. Запустите auth_spotify.py заново и вставьте полный свежий callback URL."
        if error.error == "invalid_scope":
            return "Spotify: не выданы права на библиотеку. Повторите авторизацию через auth_spotify.py."
        return "Spotify: ошибка OAuth. Проверьте Redirect URI в .env и приложении; повторите auth_spotify.py."
    if isinstance(error, SpotifyException):
        if error.http_status == 403 and "premium" in str(error).lower():
            return "Spotify требует Premium у владельца приложения в Development Mode. Новый токен это не исправит. После подключения подписки доступ может появиться через несколько часов. Для разового переноса: https://music.yandex.ru/import"
        if error.http_status == 403:
            return "Spotify запретил доступ (403). Проверьте Users and Access приложения и разрешения аккаунта; это не обязательно связано с Premium."
        if error.http_status == 401:
            return "Spotify: авторизация недействительна. Повторите auth_spotify.py."
        if error.http_status == 429:
            headers = error.headers or {}
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
            if retry_after and str(retry_after).isdigit():
                hours, remainder = divmod(int(retry_after), 3600)
                if hours:
                    return f"Spotify: лимит запросов. Повторите примерно через {hours} ч {remainder // 60} мин."
                return f"Spotify: лимит запросов. Повторите через {max(1, remainder // 60)} мин."
            return "Spotify: лимит запросов. Подождите и повторите позже."
        if error.http_status >= 500:
            return "Spotify временно недоступен. Повторите позже."
        return f"Spotify отклонил запрос (HTTP {error.http_status}). Проверьте параметры и доступность трека."
    if isinstance(error, UnauthorizedError):
        return "Яндекс: токен недействителен или отозван. Обновите YANDEX_MUSIC_TOKEN в .env и перезапустите бота."
    if isinstance(error, (RequestException, NetworkError, ConnectionError, TimeoutError)):
        return "Не удалось связаться с музыкальным сервисом. Проверьте сеть и повторите позже."
    if isinstance(error, EOFError):
        return "Авторизация требует терминала: запустите .venv/bin/python auth_spotify.py вручную."
    if isinstance(error, OSError):
        return "Не удалось прочитать или сохранить файл. Проверьте права на папку проекта и свободное место."
    if isinstance(error, (ValueError, RuntimeError)):
        return str(error)
    return f"Не удалось завершить операцию ({type(error).__name__}). Проверьте журнал службы."
