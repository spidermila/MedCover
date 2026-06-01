"""Abstract base class for admin digest blocks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseBlock(ABC):
    """Every digest block must subclass this and implement collect()."""

    # Unique snake_case identifier stored in DigestBlock.block_type
    block_type: str

    # Czech display label shown in the settings UI
    label: str

    # Short Czech description shown as a tooltip on the block type badge
    description: str = ""
    # Defaults merged into DigestBlock.config_json when the block is first seeded.
    # Must include at least {"title": "<Czech heading>"}.
    default_config: dict[str, Any] = {}

    # Path to the Jinja2 email partial template (relative to templates/)
    template: str

    @abstractmethod
    def collect(self, db_session: Any, config: dict[str, Any]) -> dict[str, Any]:
        """Query the DB and return a context dict for the email template.

        Return an empty dict (or set a "skip" key to True) to omit the block
        from the rendered digest even if it is enabled.
        """
