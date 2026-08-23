import json

from pydantic import BaseModel, Field, validator


class NostrLoginSettings(BaseModel):
    id: int = 1
    relays: list[str] = Field(
        default_factory=lambda: [
            "wss://nos.lol",
            "wss://relay.damus.io",
            "wss://relay.primal.net",
            "wss://offchain.pub",
        ]
    )
    allow_auto_user_creation: bool = False
    sync_profile_pictures: bool = True
    enable_diagnostic_logging: bool = False
    signer_app_image: str | None = Field(
        default="https://avatars.githubusercontent.com/u/63878660?s=200&v=4"
    )

    @validator("relays", pre=True)
    @classmethod
    def split_relay_text(cls, value):
        """Accepts a JSON blob (db round-trip) or a textarea with one relay per line."""
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
            value = [line.strip() for line in value.replace(",", "\n").splitlines()]
        return [relay for relay in value if relay]

    @validator("relays", each_item=True)
    @classmethod
    def validate_relay(cls, relay: str) -> str:
        from lnbits.utils.nostr import is_ws_url

        relay = relay.strip()
        if not is_ws_url(relay):
            raise ValueError(f"'{relay}' is not a valid websocket relay url.")
        return relay


class NostrLoginReplay(BaseModel):
    event_id: str
    expires_at: int
