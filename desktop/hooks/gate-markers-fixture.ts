// The cases of the gate markers, one list for both sides: tests/test_statusline.py runs them against
// hooks/statusline.py, line.test.ts against gateMarkers. Each case: the values of modules in config.json, the
// PreToolUse entries next to the status line (script, its options) and the markers, word for word. Python reads
// everything after the `=` as JSON, so this file holds this one export and nothing else.
export const GATE_MARKER_CASES = [
  {"modules": {"spawn_gate": "off"}, "entries": [["spawn_gate.py", "--mode=enforce"]], "markers": ["spawn gate off"]},
  {"modules": {"spawn_gate": "off"}, "entries": [["spawn_gate.py", "--mode=shadow"]], "markers": ["spawn gate off"]},
  {"modules": {"spawn_gate": "shadow"}, "entries": [["spawn_gate.py", "--mode=enforce"]], "markers": ["spawn gate shadow"]},
  {"modules": {"spawn_gate": "shadow"}, "entries": [["spawn_gate.py", "--mode=shadow"]], "markers": ["spawn gate shadow"]},
  {"modules": {"spawn_gate": "shadow"}, "entries": [["spawn_gate.py", ""]], "markers": ["spawn gate shadow"]},
  {"modules": {"spawn_gate": ""}, "entries": [["spawn_gate.py", "--mode=enforce"]], "markers": []},
  {"modules": {"spawn_gate": ""}, "entries": [["spawn_gate.py", "--mode=shadow"]], "markers": []},
  {"modules": {"spawn_gate": "enforce"}, "entries": [["spawn_gate.py", "--mode=shadow"]], "markers": []},
  {"modules": {"spawn_gate": "enforce"}, "entries": [["spawn_gate.py", "--mode=enforce"]], "markers": []},
  {"modules": {"soft_stop": "off"}, "entries": [["soft_stop.py", "--mode=enforce"]], "markers": ["soft stop off"]},
  {"modules": {"soft_stop": "shadow"}, "entries": [["soft_stop.py", "--mode=enforce"]], "markers": ["soft stop shadow"]},
  {"modules": {"spawn_gate": "shadow", "soft_stop": "off"}, "entries": [["soft_stop.py", "--mode=enforce"], ["spawn_gate.py", "--mode=enforce"]], "markers": ["spawn gate shadow", "soft stop off"]},
  {"modules": {"spawn_gate": "off", "soft_stop": "off"}, "entries": [["spawn_gate.py", "--mode=enforce"]], "markers": ["spawn gate off"]},
  {"modules": {"spawn_gate": "off"}, "entries": [], "markers": []},
  {"modules": {"spawn_gate": "shadow"}, "entries": [["spawn_gate.py", "--mode=enforce --mode=shadow"]], "markers": ["spawn gate shadow"]},
  {"modules": {"spawn_gate": "shadow"}, "entries": [["spawn_gate.py", "--mode=shadow --mode=enforce"]], "markers": ["spawn gate shadow"]},
  {"modules": {"spawn_gate": "shadow"}, "entries": [["spawn_gate.py", "--mode=Enforce"]], "markers": ["spawn gate shadow"]}
]
