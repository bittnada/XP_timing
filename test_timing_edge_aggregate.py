import csv
import os
import sqlite3
import tempfile
import unittest

from TimingEdgeAggregate import aggregate


class AggregateTest(unittest.TestCase):
    def test_numeric_sort_sum_mapping_and_delimiters(self):
        for separator in ("\t", ","):
            with self.subTest(separator=separator), tempfile.TemporaryDirectory() as directory:
                source = os.path.join(directory, "pins.csv")
                with open(source, "w", newline="") as stream:
                    writer = csv.writer(stream, delimiter=separator)
                    writer.writerow(["driver_id", "sink_id", "weight", "driver", "sink"])
                    writer.writerows([(10, 2, 2, "U10", "U,2"),
                                      (2, 10, 1, "U,2", "U10"),
                                      (10, 2, 3, "U10", "U,2"),
                                      (2, 3, 4, "U,2", "U3")])
                output = os.path.join(directory, "edges.tsv")
                summary = aggregate(source, output)
                with open(output) as stream:
                    self.assertEqual(list(csv.reader(stream, delimiter="\t")), [
                        ["driver_id", "sink_id", "weight"],
                        ["2", "3", "4.0"], ["2", "10", "1.0"], ["10", "2", "5.0"]])
                with open(summary["cell_map"]) as stream:
                    self.assertEqual(list(csv.reader(stream, delimiter="\t")), [
                        ["cell_id", "cell_name"], ["2", "U,2"], ["3", "U3"], ["10", "U10"]])
                output_max = os.path.join(directory, "max.csv")
                aggregate(source, output_max, merge="max", delimiter="comma")
                with open(output_max) as stream:
                    self.assertEqual(list(csv.reader(stream))[-1], ["10", "2", "3.0"])
                with self.assertRaises(FileExistsError):
                    aggregate(source, output)

    def test_conflicting_names_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = os.path.join(directory, "pins.csv"), os.path.join(directory, "edges.tsv")
            with open(source, "w") as stream:
                stream.write("driver_id,sink_id,weight,driver,sink\n1,2,1,U1,U2\n1,2,2,WRONG,U2\n")
            with self.assertRaises(sqlite3.IntegrityError):
                aggregate(source, output)
            self.assertFalse(os.path.exists(output))


if __name__ == "__main__":
    unittest.main()
