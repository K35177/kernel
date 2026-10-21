from dataclasses import dataclass
import os
from urllib.parse import urlsplit

from .config import ConfigurationError


def integer(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ConfigurationError(f'{name} должен быть целым числом.') from None
    if not minimum <= value <= maximum:
        raise ConfigurationError(f'{name} вне допустимого диапазона.')
    return value


@dataclass(frozen=True)
class BackendSettings:
    init_data_ttl: int = 300
    session_ttl: int = 900
    invite_ttl: int = 3600
    membership_ttl: int = 15
    reapply_cooldown: int = 300
    allowed_origins: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ('localhost', '127.0.0.1', '[::1]')
    environment: str = 'development'

    @classmethod
    def from_env(cls):
        environment = os.getenv('API_ENV', 'development')
        if environment not in ('development', 'production'):
            raise ConfigurationError('API_ENV должен быть development или production.')
        origins = tuple(x.strip() for x in os.getenv('API_ALLOWED_ORIGINS', '').split(',') if x.strip())
        for origin in origins:
            try:
                p = urlsplit(origin)
                valid = (p.hostname and not p.username and not p.password and not p.query
                         and not p.fragment and not p.path and p.scheme == 'https')
                if environment == 'development' and p.hostname in ('localhost', '127.0.0.1') and p.scheme == 'http':
                    valid = not (p.username or p.password or p.query or p.fragment or p.path)
                p.port
            except ValueError:
                valid = False
            if not valid:
                raise ConfigurationError('API_ALLOWED_ORIGINS: нужны явные origin без путей и credentials; production требует HTTPS.')
        if environment == 'production' and not origins:
            raise ConfigurationError('Для production задайте API_ALLOWED_ORIGINS с HTTPS origin.')
        hosts = tuple(x.strip() for x in os.getenv('API_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]').split(',') if x.strip())
        if not hosts or any('*' in host or '/' in host or ':' in host and host != '[::1]' for host in hosts):
            raise ConfigurationError('API_ALLOWED_HOSTS должен содержать явные имена хостов без wildcard.')
        return cls(integer('INIT_DATA_TTL_SECONDS',300,30,3600),
                   integer('AUTH_SESSION_TTL_SECONDS',900,60,3600),
                   integer('ACCESS_INVITE_TTL_SECONDS',3600,300,86400),
                   integer('MEMBERSHIP_CACHE_SECONDS',15,0,60),
                   integer('ACCESS_REAPPLY_COOLDOWN_SECONDS',300,0,86400),
                   origins, hosts, environment)
