"""Keep the pre-v2 behavioral suite as explicit rollback coverage.

The intent-first controller is exercised by test_conversation_core, including
transport, persistence, confirmation safety and provider timeouts.
"""
import os
from unittest.mock import patch
from app import config

_patches = []


def setUpModule():
    for context in [patch.object(config, 'CONVERSATION_CORE', 'legacy'),
                    patch.dict(os.environ, {'CONVERSATION_CORE': 'legacy'})]:
        context.start()
        _patches.append(context)


def tearDownModule():
    while _patches:
        _patches.pop().stop()
