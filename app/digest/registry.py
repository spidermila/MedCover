"""Registry of all available digest block types.

Import order determines the default sort order when blocks are first seeded.
"""

from app.digest.base import BaseBlock
from app.digest.blocks.audit_log import AuditLogBlock
from app.digest.blocks.backup_status import BackupStatusBlock
from app.digest.blocks.feedback_summary import FeedbackSummaryBlock
from app.digest.blocks.free_text import FreeTextBlock
from app.digest.blocks.new_users import NewUsersBlock
from app.digest.blocks.server_stats import ServerStatsBlock
from app.digest.blocks.upcoming_events import UpcomingEventsBlock
from app.digest.blocks.user_activity import UserActivityBlock

BLOCK_REGISTRY: dict[str, type[BaseBlock]] = {
    ServerStatsBlock.block_type: ServerStatsBlock,
    AuditLogBlock.block_type: AuditLogBlock,
    UpcomingEventsBlock.block_type: UpcomingEventsBlock,
    NewUsersBlock.block_type: NewUsersBlock,
    FeedbackSummaryBlock.block_type: FeedbackSummaryBlock,
    FreeTextBlock.block_type: FreeTextBlock,
    BackupStatusBlock.block_type: BackupStatusBlock,
    UserActivityBlock.block_type: UserActivityBlock,
}
