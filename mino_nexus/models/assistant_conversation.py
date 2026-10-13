"""持久化业务请求和投递引用；不保存 IM 正文、模型消息或附件内容。"""
from sqlalchemy import JSON, Boolean, Column, Integer, String, UniqueConstraint
from mino_nexus.core.database import Base


class AssistantRequest(Base):
    __tablename__ = 'assistant_requests'
    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    lane = Column(String, nullable=False, index=True)
    tool_id = Column(String, nullable=False)
    args = Column(JSON, default=dict)
    target = Column(JSON, default=dict)
    question = Column(JSON, default=dict)
    result = Column(JSON, default=dict)
    origin = Column(JSON, default=dict)
    state = Column(String, default='need_input', index=True)
    revision = Column(Integer, default=1)
    task_id = Column(String, default='')
    created_at = Column(Integer, nullable=False)
    updated_at = Column(Integer, nullable=False)
    expires_at = Column(Integer, nullable=False)


class AssistantFocus(Base):
    __tablename__ = 'assistant_focus'
    __table_args__ = (UniqueConstraint('user_id', 'lane', name='uq_assistant_focus'),)
    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False)
    lane = Column(String, nullable=False)
    request_id = Column(String, default='')
    task_id = Column(String, default='')
    generation = Column(Integer, default=0)


class AssistantPreference(Base):
    __tablename__ = 'assistant_preferences'
    __table_args__ = (UniqueConstraint('user_id', 'key', name='uq_assistant_preference'),)
    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    key = Column(String, nullable=False)
    value = Column(String, nullable=False)
    updated_at = Column(Integer, nullable=False)


class AssistantGroupPolicy(Base):
    __tablename__ = 'assistant_group_policies'
    __table_args__ = (UniqueConstraint('user_id', 'channel', 'instance_id', 'tenant', 'chat_id', name='uq_assistant_group'),)
    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    channel = Column(String, nullable=False)
    instance_id = Column(String, nullable=False)
    tenant = Column(String, default='')
    chat_id = Column(String, nullable=False)
    enabled = Column(Boolean, default=False)
    allow_execute = Column(Boolean, default=False)
    disclose_results = Column(Boolean, default=False)
    project_ids = Column(JSON, default=list)
    updated_at = Column(Integer, nullable=False)


class AssistantMessageReference(Base):
    __tablename__ = 'assistant_message_references'
    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    lane = Column(String, nullable=False)
    request_id = Column(String, default='')
    revision = Column(Integer, default=0)
    provider_message_id = Column(String, default='', index=True)
    state = Column(String, default='pending')
    created_at = Column(Integer, nullable=False)
