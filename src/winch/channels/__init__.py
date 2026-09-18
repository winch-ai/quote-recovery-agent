"""Outbound communication channels for Winch."""
from __future__ import annotations

from winch.channels.whatsapp import ChannelError, WhatsAppChannel

__all__ = ["WhatsAppChannel", "ChannelError"]
