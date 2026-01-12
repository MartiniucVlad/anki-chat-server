# config.py
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    # We remove the individual user/pass fields because they are baked into the URL now
    mongo_url: str = Field("mongodb://localhost:27017", env="MONGO_URL")
    mongo_db: str = Field(..., alias="MONGO_DB_NAME")
    secret_key: str = Field(..., env="SECRET_KEY")
    silicon_flow_api_key: str = Field(..., env="SILICON_FLOW_API_KEY")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()