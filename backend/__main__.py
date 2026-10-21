"""Local-only server. HTTPS/reverse proxy deployment is a separate action."""
import sys
from kernel.config import ConfigurationError


def main():
    try:
        from .app import create_app
        import uvicorn
        app = create_app()
    except ModuleNotFoundError as exc:
        if exc.name not in ('fastapi','uvicorn','starlette'):
            raise
        print('Не установлены зависимости backend. Выполните .venv/bin/python -m pip install -r requirements.txt', file=sys.stderr)
        raise SystemExit(2) from None
    except ConfigurationError as exc:
        print(f'Ошибка конфигурации: {exc}', file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(app, host='127.0.0.1', port=8000, access_log=False)


if __name__ == '__main__':
    main()
