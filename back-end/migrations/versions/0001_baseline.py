"""baseline: 引入 Alembic 之前由 create_all 建立的表

这一条是「基线」，不是普通迁移：它把 Alembic 之前已经存在的 schema
（users / chats / messages / documents / document_chunks / prompts）
纳入版本管理。已有数据库应该 ``alembic stamp`` 到这个版本而不是执行它
（应用启动时会自动完成，见 database.init_db）。

**这里的表结构是手抄的历史快照，不从 ``models.py`` 取。** 之前那一版直接
``Base.metadata.create_all``，于是"基线"跟着 models.py 一起漂：新库里
``documents`` 一出生就带着 ``content_hash``（那是 0006 才该加的列），
``alembic upgrade head`` 跑到 0006 就撞 ``Duplicate column name 'content_hash'``
——整条链在空库上从头跑不通。而 README 的手动部署路径写的正是这一条；只有
``init_db`` 的 create_all + stamp 那条走得通，因为它根本不执行迁移。

所以这一版把 6 张表的列逐列钉死在下面（照 7a11864 的 models.py 抄）。
``checkfirst=True`` 保留，"表已经在了"不当错误，与旧行为一致。

要改这些表的形状请写一条新迁移，**不要动这里**：动了它，已经 stamp 过的库
和以后新建的库会长出两个不同的 schema，而没有任何一条迁移能修那种分叉。

Revision ID: 0001_baseline
Revises:
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 基线只覆盖引入 Alembic 之前就存在的表，trace_spans 由 0002 显式创建
_BASELINE_TABLES = (
    "users",
    "chats",
    "messages",
    "documents",
    "document_chunks",
    "prompts",
)


def _baseline_metadata():
    """基线那 6 张表的历史形状。**故意不复用 ``models.py``**——理由见模块文档串。"""
    meta = sa.MetaData()

    sa.Table(
        "users",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("username", sa.String(100), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("hashed_password", sa.String(255), nullable=True),  # 第三方登录时可为空
        sa.Column("avatar", sa.String(500), nullable=True),
        sa.Column("provider", sa.String(50), nullable=True),  # local / github / google
        sa.Column("provider_id", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Index("ix_users_email", "email", unique=True),
        sa.Index("ix_users_username", "username", unique=True),
    )

    sa.Table(
        "chats",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.Index("ix_chats_user_id", "user_id"),
    )

    sa.Table(
        "messages",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),  # user / assistant / system
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("chat_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        # seq 保证同时间戳消息的排序确定性
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False, unique=True),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.Index("ix_messages_chat_id", "chat_id"),
    )

    sa.Table(
        "documents",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),  # 文档全文
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("chunks", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),  # indexed / processing / failed
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.Index("ix_documents_name", "name"),
        sa.Index("ix_documents_user_id", "user_id"),
    )

    sa.Table(
        "document_chunks",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),  # JSON 序列化的向量
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.Index("ix_document_chunks_document_id", "document_id"),
    )

    sa.Table(
        "prompts",
        meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),  # 含 {input} 占位
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.Index("ix_prompts_category", "category"),
        sa.Index("ix_prompts_user_id", "user_id"),
    )

    return meta


def upgrade() -> None:
    metadata = _baseline_metadata()
    metadata.create_all(
        bind=op.get_bind(),
        tables=[metadata.tables[name] for name in _BASELINE_TABLES],
        checkfirst=True,
    )


def downgrade() -> None:
    metadata = _baseline_metadata()
    metadata.drop_all(
        bind=op.get_bind(),
        tables=[metadata.tables[name] for name in _BASELINE_TABLES],
        checkfirst=True,
    )
