from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    BOT_TOKEN: str
    CHANNEL_ID: int
    CHANNEL_USERNAME: str | None = None
    DEVELOPER_TG_ID: int

    DB_HOST: str = "db"
    DB_PORT: int = 3306
    DB_USER: str
    DB_PASSWORD: str
    DB_NAME: str

    OPENAI_API_KEY: str | None = None
    OPENAI_VISION_MODEL: str = "gpt-4o-mini"

    GEMINI_API_KEY: str | None = None
    GEMINI_VISION_MODEL: str = "gemini-2.0-flash-lite"

    # Прокси для Bot API (нужен на хостинге в РФ, если api.telegram.org недоступен).
    # Пример: socks5://127.0.0.1:9050 или http://relay.example.com:8080
    TELEGRAM_PROXY: str | None = None

    # Прокси для Gemini/OpenAI (httpx). Для Gemini нужен выход в поддерживаемом регионе (EU/US).
    # Можно указать тот же адрес, что и TELEGRAM_PROXY, если прокси универсальный.
    HTTP_PROXY: str | None = None

    @property
    def db_url(self) -> str:
        return (
            f"mysql+asyncmy://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    class Config:
        env_file = ".env"

settings = Settings()