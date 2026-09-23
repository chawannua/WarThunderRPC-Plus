"""Tests for wtrpc.maps.

The ``maps`` dict is a hand-maintained literal; a duplicate key silently
drops whichever entry was written first with no error from Python. Parsing
the source with ``ast`` catches that class of mistake statically instead of
relying on someone noticing a map is misidentified in-game.
"""

from __future__ import annotations

import ast
import pathlib

MAPS_PATH = pathlib.Path(__file__).resolve().parent.parent / "wtrpc" / "maps.py"


def _maps_dict_node() -> ast.Dict:
    tree = ast.parse(MAPS_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "maps" for t in node.targets
        ):
            assert isinstance(node.value, ast.Dict)
            return node.value
    raise AssertionError("could not find the 'maps' dict literal in maps.py")


def _string_key(node: ast.expr) -> str:
    assert isinstance(node, ast.Constant) and isinstance(node.value, str)
    return node.value


def test_maps_dict_has_no_duplicate_keys():
    dict_node = _maps_dict_node()
    keys = [_string_key(k) for k in dict_node.keys]
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, f"duplicate map keys silently drop entries: {duplicates}"


def test_maps_module_dict_matches_literal_key_count():
    """The runtime dict must not have silently collapsed any literal keys."""
    from wtrpc.maps import maps

    dict_node = _maps_dict_node()
    literal_key_count = len(dict_node.keys)
    assert len(maps) == literal_key_count
