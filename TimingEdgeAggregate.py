#!/usr/bin/env python3
"""Aggregate pin edge CSV/TSV into directed cell-pair weights.

Sum preserves the total edge weight between cells. Max instead retains only
the strongest pin connection. A disk-backed table bounds Python memory usage.
"""
import argparse
import csv
import json
import math
import os
import sqlite3
import tempfile


def parse_delimiter(value):
    """Accept friendly names, a literal separator, or shell-friendly '\\t'."""
    delimiter = {"tab": "\t", "\\t": "\t", "comma": ",",
                 "semicolon": ";", "pipe": "|"}.get(value, value)
    if len(delimiter) != 1 or delimiter in ("\r", "\n", "\0", '"'):
        raise ValueError("Delimiter must be tab/comma/semicolon/pipe or one non-quote, non-newline character")
    return delimiter


def aggregate(input_path, output_path, merge="sum", delimiter="\t",
              cell_map_path=None, input_delimiter="auto"):
    """Write edges sorted numerically by (driver_id, sink_id), and ID/name map.

    The map contains the union of driver and sink IDs in the input, not all
    original design cells. Both tables use the selected output delimiter.
    """
    if merge not in ("sum", "max"):
        raise ValueError("merge must be sum or max")
    delimiter = parse_delimiter(delimiter)
    if input_delimiter != "auto":
        input_delimiter = parse_delimiter(input_delimiter)
    if cell_map_path is None:
        cell_map_path = os.path.splitext(os.fspath(output_path))[0] + "_cells" + (
            ".tsv" if delimiter == "\t" else ".csv")
    paths = [os.path.realpath(p) for p in (input_path, output_path, cell_map_path)]
    if len(set(paths)) != 3:
        raise ValueError("Input, edge output and cell map must have different paths")
    for path in (output_path, cell_map_path):
        if os.path.lexists(path):
            raise FileExistsError(path)
    count = 0
    total = 0.0
    with tempfile.TemporaryDirectory(prefix="timing_edge_merge_") as temporary:
        db = sqlite3.connect(os.path.join(temporary, "edges.sqlite"))
        try:
            db.execute("CREATE TABLE edges (src INTEGER, dst INTEGER, weight REAL, "
                       "PRIMARY KEY(src, dst)) WITHOUT ROWID")
            db.execute("CREATE TABLE cells (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            db.execute("CREATE TRIGGER check_cell_name BEFORE INSERT ON cells "
                       "WHEN EXISTS (SELECT 1 FROM cells WHERE id=NEW.id AND name<>NEW.name) "
                       "BEGIN SELECT RAISE(ABORT, 'Conflicting cell names for the same ID'); END")
            def flush(edges, cells):
                db.executemany(statement, edges)
                db.executemany("INSERT OR IGNORE INTO cells VALUES (?, ?)", cells)
                edges.clear()
                cells.clear()
            expression = "edges.weight + excluded.weight" if merge == "sum" else "MAX(edges.weight, excluded.weight)"
            statement = ("INSERT INTO edges VALUES (?, ?, ?) ON CONFLICT(src, dst) "
                         "DO UPDATE SET weight = " + expression)
            with open(input_path, newline="", encoding="utf-8-sig") as stream:
                # Both comma-separated CSV and tab-separated exports are accepted.
                header = stream.readline()
                source_delimiter = ("\t" if "\t" in header else ",") if input_delimiter == "auto" else input_delimiter
                stream.seek(0)
                reader = csv.DictReader(stream, delimiter=source_delimiter)
                required = {"driver_id", "sink_id", "weight", "driver", "sink"}
                if not required.issubset(reader.fieldnames or []):
                    raise ValueError("Input requires driver_id, sink_id, weight, driver and sink columns")
                batch = []
                cell_batch = []
                for row in reader:
                    src, dst = int(row["driver_id"]), int(row["sink_id"])
                    weight = float(row["weight"])
                    if src < 0 or dst < 0 or not math.isfinite(weight) or weight < 0:
                        raise ValueError("Invalid edge at input line %d" % reader.line_num)
                    if not row["driver"] or not row["sink"]:
                        raise ValueError("Missing cell name at input line %d" % reader.line_num)
                    batch.append((src, dst, weight))
                    cell_batch.extend(((src, row["driver"]), (dst, row["sink"])))
                    count += 1
                    total += weight
                    if len(batch) == 10000:
                        flush(batch, cell_batch)
                flush(batch, cell_batch)
            db.commit()
            unique, merged_total = db.execute("SELECT COUNT(*), SUM(weight) FROM edges").fetchone()
            merged_total = merged_total or 0.0
            if merge == "sum" and not math.isclose(total, merged_total, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("Total weight was not preserved")
            with open(output_path, "x", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream, delimiter=delimiter, lineterminator="\n")
                writer.writerow(["driver_id", "sink_id", "weight"])
                for src, dst, weight in db.execute("SELECT src, dst, weight FROM edges ORDER BY src, dst"):
                    if not math.isfinite(weight):
                        raise ValueError("Aggregated weight overflow")
                    writer.writerow([src, dst, weight])
            with open(cell_map_path, "x", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream, delimiter=delimiter, lineterminator="\n")
                writer.writerow(["cell_id", "cell_name"])
                writer.writerows(db.execute("SELECT id, name FROM cells ORDER BY id"))
            num_cells = db.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
            return dict(input_rows=count, cell_pairs=unique, merged_rows=count - unique,
                        merge=merge, input_total_weight=total, output_total_weight=merged_total,
                        output=os.path.abspath(output_path), cell_map=os.path.abspath(cell_map_path),
                        mapped_cells=num_cells, delimiter=delimiter,
                        edge_sort=["driver_id", "sink_id"], cell_map_sort="cell_id")
        finally:
            db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--merge", choices=["sum", "max"], default="sum")
    parser.add_argument("--delimiter", type=parse_delimiter, default="\t",
                        help="output separator for both tables: tab (default), comma, semicolon, pipe or a character")
    parser.add_argument("--input-delimiter", default="auto",
                        help="auto detects tab/comma; otherwise specify a separator as above")
    parser.add_argument("--cell-map", help="ID/name table path; default: OUTPUT_STEM_cells.tsv for tab output")
    args = parser.parse_args()
    print(json.dumps(aggregate(args.input, args.output, args.merge, args.delimiter,
                               args.cell_map, args.input_delimiter), indent=2))
