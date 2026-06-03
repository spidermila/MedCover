"""Free text digest block — admin-authored HTML/text content."""

from typing import Any

from app.digest.base import BaseBlock


class FreeTextBlock(BaseBlock):
    block_type = "free_text"
    label = "Volný text"
    description = (
        "Libovolný HTML obsah zadaný administrátorem — vhodný pro úvodní komentář, pokyny nebo pravidelnou zprávu."
    )
    template = "email/digest_blocks/free_text.html"
    default_config: dict[str, Any] = {
        "title": "Poznámka",
        "content": "",
    }

    def collect(self, db_session: Any, config: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": config.get("title", self.default_config["title"]),
            "content": config.get("content", ""),
        }
