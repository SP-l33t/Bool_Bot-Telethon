from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True)

    API_ID: int
    API_HASH: str
    GLOBAL_CONFIG_PATH: str = "TG_FARM"

    FIX_CERT: bool = False

    TRACK_BOT_UPDATES: bool = True

    SESSION_START_DELAY: int = 360
    SLEEP_TIME: list[int] = [7200, 10800]
    AUTO_TASK: bool = True
    SUBSCRIPTIONS_PER_CYCLE: int = 0
    REF_ID: str = '8T1K2'
    STAKING: bool = True
    MIN_STAKING_BALANCE: float = 200.0

    SESSIONS_PER_PROXY: int = 1
    USE_PROXY_FROM_FILE: bool = True
    DISABLE_PROXY_REPLACE: bool = False
    USE_PROXY_CHAIN: bool = False

    DEVICE_PARAMS: bool = False

    DEBUG_LOGGING: bool = False


settings = Settings()
