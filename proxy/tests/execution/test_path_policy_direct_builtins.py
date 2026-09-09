"""Path policy for the Direct-LLM client-side builtins:

- ``Delete`` is a file-path WRITE tool (a viewer's shared folders refuse it,
  memory files refuse it for everyone);
- ``Skill`` / ``tool_search`` are known structured tools — their natural-
  language arguments never hit the unknown-tool dangerous-command scan.
"""

from __future__ import annotations

from auth.path_policy import SecurityContext, check_tool_access

AGENT = "pa"


def _ctx(role="manager", username="alice"):
    return SecurityContext(role=role, username=username, agent=AGENT, is_admin_agent=False)


def test_delete_is_checked_as_a_write():
    decision, _ = check_tool_access("Delete", {"file_path": "/knowledge/doc.md"}, _ctx(role="viewer"))
    assert not decision.allowed
    decision, _ = check_tool_access("Delete", {"file_path": "/knowledge/memory/x.md"}, _ctx())
    assert not decision.allowed and "memory" in decision.reason
    decision, _ = check_tool_access("Delete", {"file_path": "/workspace/old.md"}, _ctx())
    assert decision.allowed
    decision, _ = check_tool_access("Delete", {"file_path": "/users/bob/workspace/x.md"}, _ctx())
    assert not decision.allowed


def test_skill_and_tool_search_arguments_are_not_command_scanned():
    for name, args in (
        ("tool_search", {"query": "how do I run rm -rf / on the server"}),
        ("Skill", {"name": "cleanup; rm -rf ~"}),
    ):
        decision, _ = check_tool_access(name, args, _ctx())
        assert decision.allowed, (name, decision.reason)
