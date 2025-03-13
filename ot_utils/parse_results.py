#!/usr/bin/env python3
# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
import pathlib
import sys
import enum

class OutputStyle(enum.Enum):
    CSV = 0

BORDER_CORNER_SEPARATOR = "+"
COLUMN_SEPARATOR = "|"

def read_contents(fpath: pathlib.Path) -> list[list[str]]:
    """ Reads the results file at the specified path and parses the data into a
    tabular format.

    Args:
        fpath (pathlib.Path): The path to the file to read the contents of.

    Returns the parsed information as a 2D matrix, where the first row contains
    the header names, and all other rows are test information.
    """
    try:
        # Read the entire file contents at once
        with open(fpath, "r") as f:
            contents = f.read().strip().splitlines()
    except FileNotFoundError as e:
        print(f"File {args.filename} was not found.")
        sys.exit(1)
    except OSError as e:
        print(f"OSError when reading file: maybe permissions are incorrect?\r\n{e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unable to read file contents.\r\n{e}")
        sys.exit(1)

    tests: list[list[str]] = []
    headers: list[str] = []
    # Remove all lines until the first empty line
    if contents and not contents[0].startswith(BORDER_CORNER_SEPARATOR):
        while contents:
            line, contents = (contents[0], contents[1:])
            if len(line.strip()) == 0:
                break
    # Parse test data from the file
    for line in contents:
        if line.startswith(BORDER_CORNER_SEPARATOR):
            continue
        values = [v.strip() for v in line.split(COLUMN_SEPARATOR)]
        values = [v for v in values if v != ""]
        # Strip the "ms" suffix
        if values[2].endswith(" ms"):
            values[2] = " ".join(values[2].split(" ")[:-1])
        if not headers:
            headers = values
        else:
            tests.append(values)

    # Remove extraneous ending headers 
    max_len = max([len(test) for test in tests])
    headers = headers[:max_len]
    return [headers] + [t[:max_len] for t in tests]

def main(fpath: pathlib.Path, output: OutputStyle, to_stdout: bool = True) -> None | str:
    """ Parses a QEMU OpenTItan Earlgrey test results output file and transforms
    the data into a specified format which is then output. Directly prints to
    stdout and so does not return anything.

    Args:
        fpath (pathlib.Path): The path to the file to read the contents of.
        output (OutputStyle): The format to output in.
        to_stdout (bool, default True): Whether to print to stdout or return a string.

    Returns: Either "None" if printing to stdout (default), or a string containing the
    formatted output.
    """
    results: list[list[str]] = read_contents(fpath)
    headers, tests = results[0], results[1:]

    # Format in the requested output style
    ret = ""
    if output is OutputStyle.CSV:
        if to_stdout:
            print(",".join(headers))
        else:
            ret += ",".join(headers) + "\r\n"
        for test in tests:
            if to_stdout:
                print (",".join(test))
            else:
                ret += ",".join(test) + "\r\n"
    
    # Return the output if not printing directly to stdout
    if not to_stdout:
        while ret.endswith("\r\n"):
            ret = ret[:-2]
        return ret


if __name__ == "__main__":
    import argparse
    from collections import defaultdict

    parser = argparse.ArgumentParser(
        description='Parses a QEMU OpenTitan Earlgrey test results output file',
    )
    parser.add_argument('filename', help="The path to the file containing the results to parse.")
    parser.add_argument('output', choices=["CSV"], help="The format the script should output in.")

    # Parse command-line arguments
    args = parser.parse_args()
    fpath = pathlib.Path(args.filename)
    output_styles = defaultdict(lambda: OutputStyle.CSV, {"CSV": OutputStyle.CSV})
    output = output_styles[args.output]

    main(fpath, output)

    sys.exit(0)
