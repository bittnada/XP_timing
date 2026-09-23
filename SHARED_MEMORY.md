# Two-DB shared memory

One master process publishes the immutable two-DB binaries into a POSIX shared-memory segment. Clients attach to that segment and only load their own seed positions.

Do not put seed paths or SHM fields in the JSON. Pass them as `Placer.py` flags.

## What is shared

Clients mmap the same `/dev/shm/dp2db_*` segment. They keep numpy views for:

- physical names and connectivity (`node_names`, `pin_names`, `flat_*`, `pin2node_map`, …)
- ID mapping arrays
- derived timing pin/net name spellings
- the on-disk `runtime/model.bin` file (page cache)

Each client still owns:

- coordinates, sizes, pin offsets, net weights
- GPU tensors
- OpenTimer RC/STA after `load_timing_model`

`htop` SHR should include most of the snapshot (~3 GB on superblue1). RSS still grows with OpenTimer + GPU.

## Start the master

Keep this process alive for the whole batch. One master only.

```sh
cd /mnt/hdd1/XP_timing_4.1/bin_goal2
python3 dreamplace/Placer.py dreamplace/examples/two_db_superblue1_goal2_u07.json \
  --shared_memory_role master \
  --shared_memory_dir results/shared_memory_twodb
```

Wait for:

```
shared snapshot published: ... name=dp2db_...
master holding dp2db_...; start clients with shared_memory_dir=...
```

The directory holds only `manifest.json`, `ready`, and `runtime/` (model.bin, DEF templates). The arrays live in `/dev/shm`.

Equivalent server entry point:

```sh
python3 dreamplace/SharedMemoryServer.py \
  --timing-db results/superblue1/timing_flow_v2/save \
  --placement-db results/superblue1/timing_flow_v2/PLACEMENT_U0.7/save \
  --mapping results/superblue1/timing_flow_v2/PLACEMENT_U0.7/ID_MAPPING \
  --output results/shared_memory_twodb
```

## Start clients

Each client must use a different seed and a different `--result_dir` / `--write_posX/Y`. Copying the same command three times writes the same outputs and wastes memory.

```sh
cd /mnt/hdd1/XP_timing_4.1/bin_goal2
python3 dreamplace/Placer.py dreamplace/examples/two_db_superblue1_goal2_u07.json \
  --shared_memory_role client \
  --shared_memory_dir results/shared_memory_twodb \
  --read_posX results/superblue1/multistart_seeds/seed_1001/clustered.posX.tsv \
  --read_posY results/superblue1/multistart_seeds/seed_1001/clustered.posY.tsv \
  --write_posX results/superblue1/multistart_runs/seed_1001/C/final.posX.tsv \
  --write_posY results/superblue1/multistart_runs/seed_1001/C/final.posY.tsv \
  --result_dir results/superblue1/multistart_runs/seed_1001/C
```

Repeat with `seed_1002`, `seed_1003`, …

`--read_posX/Y` apply only to the reduced placement DB. The timing DB ignores those flags.

## How a client finds the snapshot

1. `--shared_memory_dir` → `manifest.json` → `shm_name` (for example `dp2db_01aa5c981fe0f96b`).
2. `SharedSnapshot.attach` opens that POSIX name (`mmap` of `/dev/shm/dp2db_*`).
3. `TwoDBTiming.load` stores the handle on both param objects as `_shared_snapshot`.
4. `MakeDBAdapter.read` → `load_physical` builds numpy views at each entry's `offset` into `store.buf`.
5. `PlaceDB.initialize_from_rawdb` keeps those views for names and connectivity.
6. `load_mapping(..., store=store)` reads mapping arrays from the same buffer.

There is no pointer passed between processes. The name plus byte offsets are the address.

## Restart

If attach fails with `shared snapshot ... is gone`:

1. Ctrl+C the existing master (do not start a second one first).
2. Start the master command again. The same `shared_memory_dir` is reused.
3. Start clients only after `master holding`.

A dead or Ctrl+C'd master unlinks the segment. A crashed client must not unlink it; attach unregisters the name from Python's `resource_tracker` so process exit cannot delete the master's snapshot.

## Tests

```sh
cd /mnt/hdd1/XP_timing_4.1/bin_goal2
python3 dreamplace/test_shared_snapshot.py
```
