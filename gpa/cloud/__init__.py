"""Contracts shared by the hosted GPA service and local host agent."""

from gpa.cloud.agent_protocol import (
    AGENT_PROTOCOL_VERSION,
    AgentCommand,
    AgentProtocolError,
    build_agent_hello,
    command_requires_local_approval,
    parse_cloud_command,
)
from gpa.cloud.service_config import CloudServiceConfig, CloudServiceConfigurationError

__all__ = [
    "AGENT_PROTOCOL_VERSION",
    "AgentCommand",
    "AgentProtocolError",
    "CloudServiceConfig",
    "CloudServiceConfigurationError",
    "build_agent_hello",
    "command_requires_local_approval",
    "parse_cloud_command",
]
