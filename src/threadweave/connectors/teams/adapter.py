# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
ThreadWeave Teams Adapter — Azure AD auth and bot server setup.

Connects the ThreadWeaveTeamsBot to Microsoft Bot Framework
via Azure AD (Entra ID) authentication.

Usage:
    Set environment variables:
        MICROSOFT_APP_ID=<your-bot-app-id>
        MICROSOFT_APP_PASSWORD=<your-bot-app-password>

    Run:
        python -m threadweave.connectors.teams.adapter

Requirements:
    pip install botbuilder-core botbuilder-integration-aiohttp msal
"""

from __future__ import annotations

import logging
import os
import sys

try:
    from aiohttp import web
    from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
    from botbuilder.integration.aiohttp import (
        ConfigurationBotFrameworkAuthentication,
        CloudAdapter,
    )
    BOTBUILDER_AVAILABLE = True
except ImportError:
    BOTBUILDER_AVAILABLE = False

from threadweave.connectors.teams.bot import ThreadWeaveTeamsBot

logger = logging.getLogger(__name__)

# Default ports
DEFAULT_PORT = 3978  # Bot Framework default


def create_adapter() -> BotFrameworkAdapter | object:
    """Create a Bot Framework adapter authenticated via Azure AD.

    Returns a CloudAdapter (botframework-connector 4.15+) if available,
    falling back to BotFrameworkAdapter for older SDK versions.
    """
    if not BOTBUILDER_AVAILABLE:
        raise ImportError(
            "Bot Framework SDK not installed. "
            "Run: pip install botbuilder-core botbuilder-integration-aiohttp"
        )

    app_id = os.environ.get("MICROSOFT_APP_ID", "")
    app_password = os.environ.get("MICROSOFT_APP_PASSWORD", "")
    app_tenant = os.environ.get("MICROSOFT_APP_TENANT_ID", "")

    if not app_id or not app_password:
        logger.warning(
            "MICROSOFT_APP_ID and MICROSOFT_APP_PASSWORD not set. "
            "Bot will fail to authenticate with Teams."
        )

    # CloudAdapter's ConfigurationBotFrameworkAuthentication reads UPPERCASE
    # attributes (APP_ID, APP_PASSWORD, APP_TYPE, APP_TENANTID) off the config
    # object. Passing a BotFrameworkAdapterSettings (lowercase attrs) silently
    # produces an unauthenticated adapter that accepts ANY request — fixed
    # 2026-08-05 during live Teams testing.
    class _BotConfig:
        APP_TYPE = os.environ.get("MICROSOFT_APP_TYPE", "MultiTenant")
        APP_ID = app_id
        APP_PASSWORD = app_password
        APP_TENANTID = app_tenant

    settings = BotFrameworkAdapterSettings(
        app_id=app_id,
        app_password=app_password,
    )

    # Try CloudAdapter first (modern), fall back to BotFrameworkAdapter
    try:
        if "CloudAdapter" in globals():
            auth = ConfigurationBotFrameworkAuthentication(_BotConfig)
            return CloudAdapter(auth)
    except Exception:
        pass

    return BotFrameworkAdapter(settings)


def create_app(
    api_base_url: str = "http://localhost:8000",
    mode: str = "both",
) -> web.Application:
    """Create an aiohttp web application serving the Teams bot.

    Args:
        api_base_url: URL of the ThreadWeave API server.
        mode: Bot operation mode ("passive", "explicit", "both").
    """
    adapter = create_adapter()
    bot = ThreadWeaveTeamsBot(api_base_url=api_base_url, mode=mode)

    app = web.Application()

    # Start the capture-notification poller with the server ("camera
    # sign" DMs). Stopped on shutdown to avoid dangling tasks.
    async def _start_poller(app):
        bot.start_notification_poller()

    async def _stop_poller(app):
        await bot.stop_notification_poller()

    app.on_startup.append(_start_poller)
    app.on_shutdown.append(_stop_poller)

    # Bot Framework messaging endpoint — POST /api/messages.
    # Modern botbuilder (>=4.15) dropped the aiohttp_channel_service_routes
    # helper; the canonical pattern is adapter.process(request, bot).
    async def messages(request: web.Request) -> web.Response:
        return await adapter.process(request, bot)

    app.router.add_post("/api/messages", messages)

    # Health check endpoint
    async def health(request: web.Request) -> web.Response:
        return web.json_response({
            "status": "healthy",
            "bot_mode": mode,
            "stats": bot.stats,
        })

    app.router.add_get("/health", health)

    return app


def main():
    """Entry point for running the Teams bot server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    port = int(os.environ.get("PORT", DEFAULT_PORT))
    api_url = os.environ.get("THREADWEAVE_API_URL", "http://localhost:8000")
    mode = os.environ.get("THREADWEAVE_BOT_MODE", "both")

    logger.info("Starting ThreadWeave Teams Bot")
    logger.info("  API: %s", api_url)
    logger.info("  Mode: %s", mode)
    logger.info("  Port: %s", port)

    app = create_app(api_base_url=api_url, mode=mode)

    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
