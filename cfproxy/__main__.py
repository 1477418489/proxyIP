import uvicorn

from .app import create_app
from .config import Settings


def main():
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1, proxy_headers=False)


if __name__ == "__main__":
    main()
