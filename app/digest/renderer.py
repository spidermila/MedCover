"""Digest renderer — collects data from all enabled blocks and renders the HTML email."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from flask import render_template

from app.digest.registry import BLOCK_REGISTRY


def render_digest(db_session: Any) -> str:
    """Render the full admin digest as an HTML string."""
    from app.models.digest import get_digest_schedule  # pylint: disable=import-outside-toplevel
    from app.utils import get_app_tz  # pylint: disable=import-outside-toplevel

    schedule = get_digest_schedule()
    block_sections: list[str] = []

    now = datetime.now(get_app_tz())

    for db_block in schedule.blocks:  # type: ignore[attr-defined]
        if not db_block.enabled:
            continue
        cls = BLOCK_REGISTRY.get(db_block.block_type)
        if cls is None:
            continue
        instance = cls()
        config: dict[str, Any] = db_block.config_json or {}
        try:
            context = instance.collect(db_session, config)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("Digest block %s collect() failed", db_block.block_type)
            continue
        if context.get("skip"):
            continue
        section_html = render_template(instance.template, **context)
        block_sections.append(section_html)

    return render_template(
        "email/admin_digest.html",
        schedule=schedule,
        block_sections=block_sections,
        now=now,
    )
