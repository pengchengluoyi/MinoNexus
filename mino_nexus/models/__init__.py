"""ORM：一张表一个文件。这里的类名对应 mino.db 的表，改字段要同步 docs/DATA_MODEL.md。"""
from mino_nexus.models.node_tool_task import NodeToolTask
from mino_nexus.models.account_fact_candidate import AccountFactCandidate
from mino_nexus.models.app_intel import AppIntelLink, AppIntelProposal
from mino_nexus.models.assistant import (
    AssistantMcpToken,
    AssistantProfile,
    AssistantSubscription,
    AssistantTurn,
    ImBindCode,
    ImIdentity,
)
from mino_nexus.models.atlas import AtlasAlias
from mino_nexus.models.auth import AuthSession, AuthState, User
from mino_nexus.models.case import ProjectCase
from mino_nexus.models.case_script import CaseScript
from mino_nexus.models.case_platform_script import CasePlatformScript
from mino_nexus.models.project_script_repo import ProjectScriptRepo
from mino_nexus.models.case_source import CaseSource, CaseSourceRead, CaseSourceRow
from mino_nexus.models.run_admission import RunAdmission
from mino_nexus.models.execution_resource import ExecutionResource, ResourceDomain
from mino_nexus.models.case_import_preview import CaseImportPreview
from mino_nexus.models.case_import_receipt import CaseImportReceipt
from mino_nexus.models.case_import_receipt_line import CaseImportReceiptLine
from mino_nexus.models.catalog import CatalogEntry, CatalogMeta
from mino_nexus.models.device_reservation import DeviceReservation
from mino_nexus.models.external_lease import ExternalLease
from mino_nexus.models.device import Device
from mino_nexus.models.device_app_session import DeviceAppSession
from mino_nexus.models.resource_ops import (
    DeviceResourceLease,
    ResourceAllocationLog,
    ResourceTransitionAudit,
    ResourceTransitionRule,
)
from mino_nexus.models.dispatch import DispatchCall
from mino_nexus.models.model_usage import ModelUsage
from mino_nexus.models.prompt_evidence import PromptEvidence
from mino_nexus.models.assistant_conversation import AssistantRequest, AssistantFocus, AssistantPreference, AssistantGroupPolicy, AssistantMessageReference
from mino_nexus.models.assistant_delivery import AssistantReceipt, AssistantNotification
from mino_nexus.models.doc_library import DocChunk, DocSource
from mino_nexus.models.knowledge import KnowledgeEntry
from mino_nexus.models.nav_fsm import NavFsm, NavFsmEdge, NavFsmState
from mino_nexus.models.nav_page_entry import NavPageEntry, NavPageEntryEvidence, NavPageGap
from mino_nexus.models.nav_case_page_ref import NavCasePageRef
from mino_nexus.models.nav_flow_block_catalog import NavFlowBlockCatalog
from mino_nexus.models.node import Node, Studio
from mino_nexus.models.plugin import PluginPolicy, UserPluginSecret
from mino_nexus.models.pool_account import PoolAccount, PoolAccountFacet
from mino_nexus.models.project import App, Project
from mino_nexus.models.project_pool_config import ProjectPoolConfig
from mino_nexus.models.run import AppRegressionRun
from mino_nexus.models.session_event import SessionEvent, SessionMeta
from mino_nexus.models.settings import Settings
from mino_nexus.models.token import InstallToken, NodeCredential
from mino_nexus.models.ui_control import UiControl

__all__ = [
    "ExternalLease",
    "AssistantMcpToken",
    "AssistantProfile",
    "AssistantSubscription",
    "AssistantTurn",
    "ImBindCode",
    "ImIdentity",
    "AppIntelLink",
    "AppIntelProposal",
    "App",
    "ProjectCase",
    "CaseScript",
    "CasePlatformScript",
    "ProjectScriptRepo",
    "CaseRevision",
    "CaseCompileJob",
    "CaseImportPreview",
    "CaseImportReceipt",
    "CaseImportReceiptLine",
    "AppRegressionRun",
    "AtlasAlias",
    "AuthSession",
    "AuthState",
    "CatalogEntry",
    "CatalogMeta",
    "Device",
    "DeviceAppSession",
    "DispatchCall",
    "DocChunk",
    "DocSource",
    "KnowledgeEntry",
    "InstallToken",
    "NodeCredential",
    "NavFsm",
    "NavFsmEdge",
    "NavFsmState",
    "NavPageEntry",
    "NavPageEntryEvidence",
    "NavPageGap",
    "NavCasePageRef",
    "NavFlowBlockCatalog",
    "Node",
    "PluginPolicy",
    "PoolAccount",
    "PoolAccountFacet",
    "ProjectPoolConfig",
    "Project",
    "SessionEvent",
    "SessionMeta",
    "Settings",
    "Studio",
    "User",
    "UserPluginSecret",
]

from mino_nexus.models.case_revision import CaseRevision
from mino_nexus.models.case_compile_job import CaseCompileJob

from mino_nexus.models.scout_update import ScoutUpdate

from mino_nexus.core.database import register_model_loader as _register_model_loader  # noqa: E402


def _models_ready() -> None:
    return None


_register_model_loader(_models_ready)
