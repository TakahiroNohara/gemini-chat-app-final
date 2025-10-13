"""add composite indexes on conversation and message

Revision ID: add_composite_indexes_20251013
Revises:
Create Date: 2025-10-13
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'add_composite_indexes_20251013'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # conversation(user_id, is_pinned, id)
    op.create_index(
        'ix_conversation_user_pinned_id',
        'conversation',
        ['user_id', 'is_pinned', 'id'],
        unique=False
    )

    # message(conversation_id, id)
    op.create_index(
        'ix_message_conversation_id_id',
        'message',
        ['conversation_id', 'id'],
        unique=False
    )


def downgrade():
    op.drop_index('ix_message_conversation_id_id', table_name='message')
    op.drop_index('ix_conversation_user_pinned_id', table_name='conversation')
