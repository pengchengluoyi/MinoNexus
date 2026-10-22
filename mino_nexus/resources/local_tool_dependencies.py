"""Ordinary local tools share menu and dispatch dependency policy; no tool I/O."""
from mino_nexus.resources.execution_dependencies import fact, model_fact, plugin_fact


def evaluate(capability_id, *, ctx, shot=None, execution=False):
    if capability_id not in {"assert_visual", "get_otp"}:
        return []
    from mino_nexus.platform.identity import require_user_id
    uid = require_user_id()
    owner = str(getattr(ctx, "plugin_user_id", "") or "")
    if owner and owner != uid:
        raise PermissionError("execution_owner_mismatch")
    if capability_id == "assert_visual":
        facts = [model_fact(uid, str(getattr(ctx, "provider_id", "") or ""),
                            explicit_selection=bool(getattr(ctx, "provider_id", "")))]
        # A menu is assembled before capture; only dispatch requires fresh evidence.
        if execution:
            present = shot is not None and callable(getattr(shot, "has_image", None)) and shot.has_image()
            facts.append(fact("ready", "observation_available") if present
                         else fact("blocked", "observation_missing"))
        return facts
    from mino_nexus.resources.otp_resolve import lease_email_for_otp as _lease_email_for_otp, resolve_otp as _resolve_otp
    code, source = _resolve_otp(ctx)
    if code:
        return [fact("ready", "otp_source_configured")]
    if source != "gmail":
        return [fact("needs_input", "otp_human_input_required" if source == "hitl" else "otp_source_missing")]
    from mino_nexus.resources.node_registry import get_registry
    reg = get_registry()
    node, _ = reg.resolve(str(getattr(ctx, "sn", "") or ""))
    if node is None:
        return [fact("blocked", "plugin_node_unavailable")]
    facts = [plugin_fact(reg.nodes(), {"kind": "plugin", "class": "mail", "plugin_id": "gmail",
                                      "node_id": node.node_id}, uid)]
    if not _lease_email_for_otp(ctx):
        facts.append(fact("needs_input", "otp_recipient_missing"))
    if execution and float(getattr(ctx, "otp_sent_at", 0) or 0) <= 0:
        facts.append(fact("blocked", "otp_send_window_missing"))
    return facts
