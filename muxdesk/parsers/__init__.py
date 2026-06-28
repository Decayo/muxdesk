"""Pluggable transcript parsers with antifragile fallback.

Providers implement the :class:`~muxdesk.parsers.base.Parser` interface for
different AI CLI transcript formats; :class:`~muxdesk.parsers.chain.ParserChain`
composes them so a failed primary source degrades to a fallback rather than
losing the conversation. See openspec change ``add-parser-multi-provider``.
"""
from muxdesk.parsers.base import Parser
from muxdesk.parsers.chain import ParserChain
from muxdesk.parsers.claude import ClaudeParser
from muxdesk.parsers.codex import CodexParser
from muxdesk.parsers.pane import PaneParser

__all__ = ["Parser", "ClaudeParser", "CodexParser", "PaneParser", "ParserChain"]
