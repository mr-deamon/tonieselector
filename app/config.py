from pathlib import Path
import json

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Tonie Selector"
    data_root: Path = Path("data")
    db_path: Path = Path("data/tonieselector.sqlite3")
    my_tonies_base_url: str = "https://api.prod.tcs.toys/v2"
    my_tonies_graphql_url: str = "https://api.prod.tcs.toys/v2/graphql"
    my_tonies_api_token: str = ""
    my_tonies_auth_base_url: str = "https://login.tonies.com"
    my_tonies_client_id: str = "my-tonies"
    my_tonies_redirect_uri: str = "https://my.tonies.com/login"
    my_tonies_scope: str = "openid"
    my_tonies_ui_locales: str = ""
    my_tonies_username: str = ""
    my_tonies_password: str = ""
    default_figure_id: str = ""
    figure_options: str = ""
    figure_whitelist: str = ""
    figure_blacklist: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()


def parse_figure_list(raw_value: str) -> list[str]:
    """Parse a comma-separated list of figure IDs into a Python list."""
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def parse_figure_options(raw_value: str) -> list[dict[str, str]]:
    raw = raw_value.strip()
    if not raw:
        return []

    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        options: list[dict[str, str]] = []
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                figure_id = str(item.get("id", "")).strip()
                if not figure_id:
                    continue
                name = str(item.get("name", figure_id)).strip() or figure_id
                image_url = str(item.get("image_url") or item.get("imageUrl") or "").strip()
                option = {"id": figure_id, "name": name}
                if image_url:
                    option["image_url"] = image_url
                options.append(option)
        return options

    options: list[dict[str, str]] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        bits = [segment.strip() for segment in item.split(":")]
        figure_id = bits[0] if bits else ""
        if not figure_id:
            continue
        label = bits[1] if len(bits) > 1 else figure_id
        image_url = ":".join(bits[2:]).strip() if len(bits) > 2 else ""
        option = {"id": figure_id, "name": label or figure_id}
        if image_url:
            option["image_url"] = image_url
        options.append(option)
    return options
