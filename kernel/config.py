from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import os

from dotenv import load_dotenv
from aiogram.utils.token import TokenValidationError, validate_token

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigurationError(ValueError):
    """A diagnostic containing variable names and instructions, never values."""


def required(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise ConfigurationError(f'{name} не задан. Проверьте окружение процесса и существующий .env.')
    if not value.strip():
        raise ConfigurationError(f'{name} пуст. Укажите рабочее значение в окружении или .env.')
    return value


def telegram_id(name: str, *, positive: bool) -> int:
    raw = required(name)
    diagnostic = f'{name} должен быть ' + ('положительным' if positive else 'отрицательным') + ' целым числом.'
    try:
        value = int(raw)
    except ValueError:
        raise ConfigurationError(diagnostic) from None
    if (value <= 0 if positive else value >= 0):
        raise ConfigurationError(diagnostic)
    return value


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    root_group_id: int
    owner_id: int
    database_path: Path
    mini_app_url: str | None = None
    timezone: str = 'Europe/Moscow'

    @classmethod
    def from_env(cls) -> 'Settings':
        # Read only: never create, rewrite, repair or replace an existing .env.
        # Keep the existing precedence: process environment overrides the file.
        token_from_process = 'BOT_TOKEN' in os.environ
        try:
            load_dotenv(PROJECT_ROOT / '.env', override=False)
        except (OSError, UnicodeError, ValueError):
            raise ConfigurationError(
                'Не удалось загрузить .env. Проверьте доступ, кодировку UTF-8 и формат значений.'
            ) from None
        token = required('BOT_TOKEN')
        try:
            validate_token(token)
        except TokenValidationError:
            source = 'окружения процесса (оно имеет приоритет над .env)' if token_from_process else '.env'
            raise ConfigurationError(
                f'BOT_TOKEN из {source} имеет некорректный формат. '
                'Проверьте рабочий токен; значение скрыто, .env не изменён.'
            ) from None
        owner_id = telegram_id('OWNER_ID', positive=True)
        group_id = telegram_id('ROOT_GROUP_ID', positive=False)
        url = os.getenv('MINI_APP_URL', '').strip() or None
        if url:
            try:
                parts = urlsplit(url)
                valid = (parts.scheme == 'https' and parts.hostname and not parts.username
                         and not parts.password)
                parts.port  # Validate malformed or out-of-range ports, too.
            except ValueError:
                valid = False
            if not valid:
                raise ConfigurationError('MINI_APP_URL должен быть HTTPS URL без логина и пароля.')
        timezone = os.getenv('TIMEZONE', 'Europe/Moscow')
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigurationError('TIMEZONE должен содержать существующий часовой пояс IANA.') from None
        raw_path = os.getenv('DATABASE_PATH', 'data/kernel.sqlite3')
        if not raw_path.strip() or '\x00' in raw_path:
            raise ConfigurationError('DATABASE_PATH должен содержать непустой путь к файлу SQLite.')
        try:
            path = Path(raw_path).expanduser()
        except (ValueError, RuntimeError):
            raise ConfigurationError('DATABASE_PATH содержит некорректный путь к файлу SQLite.') from None
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return cls(token, group_id, owner_id, path, url, timezone)
