"""The Claude CLI truncates MCP tool descriptions at 2048 characters, so
anything past that never reaches the model. write_xlsx moved its parameter
shapes into the `help` op to fit; the tools still over the cap are listed
here so a future change to that set is deliberate."""

import ast
from pathlib import Path

_CAP = 2048
_KNOWN_OVER_CAP = {"write_docx", "write_pptx"}


def _descriptions() -> dict[str, str]:
    src = (Path(__file__).parent.parent / "server.py").read_text()
    out = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Tool":
            kw = {k.arg: k.value for k in node.keywords}
            out[ast.literal_eval(kw["name"])] = ast.literal_eval(kw["description"])
    return out


def test_write_xlsx_description_fits_the_cli_cap():
    d = _descriptions()["write_xlsx"]
    assert len(d) <= _CAP - 40, len(d)  # margin for a stray edit
    assert '{"op": "help"}' in d and "anchor" in d and "DATES" in d


def test_over_cap_tools_are_the_known_ones():
    over = {name for name, d in _descriptions().items() if len(d) > _CAP}
    assert over == _KNOWN_OVER_CAP
