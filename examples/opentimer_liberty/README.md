# OpenTimer Liberty LUT regression

The local OpenTimer fix is in `thirdparty/OpenTimer/ot/liberty/celllib.cpp`,
`Celllib::_extract_lut()`. Tables inherit omitted axes from their template;
explicit axes override it independently. Values are collected safely before
validating the final dimensions, so attribute ordering does not matter. Scalar
and 1D tables retain a singleton unused dimension. Missing/duplicate fields,
missing templates and mismatched value counts fail explicitly.

After rebuilding the OpenTimer static library, run from `dreamplace`:

```sh
python3 -m unittest test_opentimer_liberty -v
```

The test compiles `read_probe.cpp` against the project's static library using
its C++ ABI setting (`_GLIBCXX_USE_CXX11_ABI=0`). To test an instrumented probe,
set `OPENTIMER_LIBERTY_PROBE=/absolute/path/to/read_probe_asan`. Invalid-input
tests run in separate processes with core dumps disabled.

For the installed Python binding, run from `bin`:

```sh
python3 dreamplace/TestOpenTimerLib.py --input /path/to/input.lib
```

## Bus pins and conditional timing

Quoted tokens and bracketed names are preserved. Explicit ranges such as
`pin(DO[0:31])` and `pin("DI[31:0]")` expand into distinct scalar pins, copying
their attributes and timing groups. Duplicate expanded names are errors, not
silent overwrites. Related-pin lists/ranges expand into separate timing groups;
references to nonexistent scalar pins are rejected. Type-only buses without
explicit indexed members, `related_bus_pins` and timing `mode` attributes are
currently rejected instead of silently dropped.

`when` and `sdf_cond` are retained in memory, dumps, timing identity comparisons
and binary cache schema 2. They are **not evaluated as logic/case analysis**.
Without mode filtering, STA considers all conditional arcs; loading such a
library emits an explicit warning. This may be pessimistic and is not a
mode-specific timing result. CCS current tables and memory functional semantics
are not added by this patch.

`dump_timer` now reports library pin/input/output and conditional timing-group
counts separately from design instance pin/arc counts. A LIB-only test still
has zero netlist gates/nets. `TestOpenTimerLib.py` explicitly identifies its
success as parsing, not mode-specific STA validation.

Existing timing cache format 1 is rejected; regenerate the timing DB using the
existing explicit `db_option=def, mode=binary_write` workflow and a new save path.
Existing LIB/DB files are not rewritten automatically. Physical DB structure is
unchanged. Successful parsing does not prove complete STA compatibility.
