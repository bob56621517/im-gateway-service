"""Control-plane HTTP API (FastAPI) and ChannelManager wiring."""

from __future__ import annotations

from im_gateway.gateway.app import create_app, run_daemon
from im_gateway.gateway.dev_processor import echo_processor
from im_gateway.gateway.events import EventBus
from im_gateway.gateway.factory import build_config, constraints_of
from im_gateway.gateway.runtime import GatewayRuntime

__all__ = [
    "EventBus",
    "GatewayRuntime",
    "build_config",
    "constraints_of",
    "create_app",
    "echo_processor",
    "run_daemon",
]
