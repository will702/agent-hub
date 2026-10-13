from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://agenthub:agenthub@127.0.0.1:5433/agenthub"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765
    dashboard_port: int = 8766
    embedder: str = "fastembed"
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    lease_minutes: int = 15
    skills_dir: Path = ROOT / "shared" / "skills"
    agents_dir: Path = ROOT / "agents"
    policies_dir: Path = ROOT / "shared" / "policies"


settings = Settings()
