"""Gmail tools are classified by what they actually do, not by their name."""

from __future__ import annotations

import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.security.capabilities import DEFAULT_TOOL_CAPABILITIES, Capability
from openjarvis.security.data_boundary_audit import (
    CHANNEL_OUTBOUND_TOOLS,
    CONNECTOR_NETWORK_TOOLS,
    LOCAL_ACCESS_TOOLS,
    build_data_boundary_report,
)

READS = ("gmail_search_emails", "gmail_get_thread")
DRAFT = "gmail_create_draft"
APPROVAL = "gmail_request_send_approval"


def _finding(tmp_path, tool):
    config = JarvisConfig()
    config.tools.enabled = tool
    report = build_data_boundary_report(config, tmp_path)
    return {f.id: f for f in report.findings}


@pytest.mark.parametrize("tool", [*READS, DRAFT])
def test_live_gmail_tools_are_connector_network_tools(tool):
    assert tool in CONNECTOR_NETWORK_TOOLS
    assert tool not in LOCAL_ACCESS_TOOLS
    assert DEFAULT_TOOL_CAPABILITIES[tool] == [Capability.NETWORK_FETCH]


@pytest.mark.parametrize("tool", READS)
def test_reads_disclose_content_to_the_ai_model(tmp_path, tool):
    finding = _finding(tmp_path, tool)["connector-network-tools-configured"]
    assert finding.status == "warn"
    path = finding.potential_data_path
    assert "Google" in path and "AI model" in path and "hosted" in path


def test_draft_is_a_write_to_gmail_and_not_a_send(tmp_path):
    finding = _finding(tmp_path, DRAFT)["connector-network-tools-configured"]
    assert "Gmail drafts" in finding.potential_data_path
    assert "not sent" in finding.potential_data_path
    assert "AI model" not in finding.potential_data_path
    assert DRAFT not in CHANNEL_OUTBOUND_TOOLS
    assert Capability.CHANNEL_SEND not in DEFAULT_TOOL_CAPABILITIES[DRAFT]


def test_approval_request_is_local_only_and_never_a_send(tmp_path):
    assert APPROVAL in LOCAL_ACCESS_TOOLS
    assert APPROVAL not in CONNECTOR_NETWORK_TOOLS | CHANNEL_OUTBOUND_TOOLS
    assert DEFAULT_TOOL_CAPABILITIES[APPROVAL] == [Capability.MEMORY_WRITE]
    findings = _finding(tmp_path, APPROVAL)
    assert "connector-network-tools-configured" not in findings
    assert "channel-outbound-tool-configured" not in findings


def test_no_gmail_tool_holds_the_send_capability():
    for tool in (*READS, DRAFT, APPROVAL):
        assert Capability.CHANNEL_SEND not in DEFAULT_TOOL_CAPABILITIES[tool]
