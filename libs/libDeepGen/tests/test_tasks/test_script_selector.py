from libDeepGen.tasks import ScriptSelector
from pathlib import Path

def test_script_selector():
    summary_json = Path.home() / "aixcc/libDeepGen/workdir-libDeepGen/summary.json"
    selector = ScriptSelector(str(summary_json))
    x, y, z = selector.pick_next_script()
    assert x is not None
    assert y is not None
    assert z is not None
    x2, y2, z2 = selector.pick_next_script()
    assert x == x2
    assert y == y2
    assert z == z2
    print(x, y)
    print(x2, y2)
