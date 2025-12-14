# config.py
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    mongo_user: str = Field(..., env="MONGO_USER")
    mongo_password: str = Field(..., env="MONGO_PASSWORD")
    mongo_host: str = Field(..., env="MONGO_HOST")
    mongo_db: str = Field(..., env="MONGO_DB")

    @property
    def mongo_url(self) -> str:
        return (
            f"mongodb+srv://{self.mongo_user}:{self.mongo_password}"
            f"@{self.mongo_host}/?retryWrites=true&w=majority"
        )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
