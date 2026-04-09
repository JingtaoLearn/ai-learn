#!/usr/bin/env python3
"""Convenience entry point for seeding the EMS database.

Usage:
    python seed_data.py
    # or inside Docker:
    docker compose exec experience-manager python seed_data.py
"""

from scripts.seed import seed

if __name__ == "__main__":
    import asyncio
    asyncio.run(seed())
