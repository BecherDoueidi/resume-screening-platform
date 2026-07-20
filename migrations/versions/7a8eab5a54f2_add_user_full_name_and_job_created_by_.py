"""add user full_name and job created_by_user_id

Revision ID: 7a8eab5a54f2
Revises: 017464b277d3
Create Date: 2026-07-20 13:56:56.607480

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a8eab5a54f2'
down_revision: Union[str, Sequence[str], None] = '017464b277d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch mode: SQLite can't ALTER TABLE to add a constraint directly (it
    # rebuilds the table under the hood); this is a correct no-extra-cost
    # passthrough on Postgres too, so one migration works on both.
    with op.batch_alter_table('job_positions') as batch_op:
        batch_op.add_column(sa.Column('created_by_user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_job_positions_created_by_user_id_users', 'users', ['created_by_user_id'], ['id'], ondelete='SET NULL'
        )
    op.add_column('users', sa.Column('full_name', sa.String(length=200), nullable=False, server_default=''))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'full_name')
    with op.batch_alter_table('job_positions') as batch_op:
        batch_op.drop_constraint('fk_job_positions_created_by_user_id_users', type_='foreignkey')
        batch_op.drop_column('created_by_user_id')
