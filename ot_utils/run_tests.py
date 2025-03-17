#!/usr/bin/env python3
# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
import pathlib
import sys
import os
import glob
import signal
import subprocess
import multiprocessing
import time
from typing import List, Union, Optional, Tuple
from enum import Enum

# Parameters for general operation
DEBUG_LOG = False
STREAM_TEST_OUTPUT = False
LAST_N_LINES = 4
EXIT_ON_FAILURE = True

# Directory to create the scripts in. Relative to this python file in the Renode repo.
SCRIPTS_DIR = pathlib.Path(__file__).parent.parent.joinpath("scripts")
# Directory to create and place Renode .resc scripts in when running tests.
TEST_SCRIPTS_DIR = SCRIPTS_DIR.joinpath("ot_tests")

# Timeout configuration options.
DEFAULT_TIMEOUT_SECS = 60
USE_TIMEOUT_OVERRIDES = True
TIMEOUT_OVERRIDES = {
    "mod_exp_otbn_functest_wycheproof_fpga_cw310_test_rom": 30000,
    "mod_exp_ibex_functest_wycheproof_fpga_cw310_test_rom": 30000,
    "rsa_3072_verify_functest_wycheproof_fpga_cw310_test_rom": 30000,
    "retention_sram_functest_fpga_cw310_test_rom": 30000,
}

# The writer/reader synchronisation mechanisms currently just use a basic
# Busy-wait polling mechanism rather than any synchronisation primitives.
# This defines the wait intervals for those loops.
WRITE_POLL_TIME_SEC = 0.1
READ_POLL_TIME_SEC = 0.1

# Renode Command-Line Interface Information
RENODE_CMD = "./renode"
RENODE_ARGS = "--console --disable-gui --plain"
TEST_CMD = "start @scripts/{}/{}"
RENODE_INIT_SUFFIX_STR = ")\n\n"

# OpenTitan Test Information
STDOUT_STRS = {
    "PASS": "PASS",
    "FAIL": "FAIL",
    "FAULT": "FAULT",
}

# Formatting Parameters and Colour Configuration
TEST_INDENTATION = 2
TEXT_COLOURS = {
    "RED": "\033[91m",
    "GREEN": "\033[92m",
    "ORANGE": "\033[93m",
    "BLUE": "\033[94m",
    "ENDC": "\033[0m",
}
TEXT_COLOURS["INFO"] = TEXT_COLOURS["BLUE"]
TEXT_COLOURS["PASS"] = TEXT_COLOURS["GREEN"]
TEXT_COLOURS["FAIL"] = TEXT_COLOURS["RED"]
TEXT_COLOURS["ERROR"] = TEXT_COLOURS["RED"]
TEXT_COLOURS["TIMEOUT"] = TEXT_COLOURS["ORANGE"]
TEXT_COLOURS["WARNING"] = TEXT_COLOURS["ORANGE"]
ENDC = TEXT_COLOURS["ENDC"]

# Default Renode .resc script for initializing an OpenTitan Earlgrey test in Renode
TEST_SCRIPT = """
:name: OpenTitan Earlgrey
:description: This script runs an Opentitan Earlgrey test at commit f243e6802143374741739d2c164c4f2f61697669.

$name?="EarlGrey"

using sysbus
mach create $name
machine LoadPlatformDescription @platforms/cpus/opentitan-earlgrey-cw310.repl

showAnalyzer sysbus.uart0

$boot?=@https://dl.antmicro.com/projects/renode/test_rom_fpga_cw310.elf-s_447072-1cdfd7b2a98b0c09f158d8267c5e9fbbf34dd33b
$boot_vmem?=@https://dl.antmicro.com/projects/renode/test_rom_fpga_cw310.39.scr.vmem-s_103772-d3a8f17879eedbcbf18e554bfd7871ccd992414e
$otp_vmem?=@https://dl.antmicro.com/projects/renode/open_titan-earlgrey--otp-img.24.vmem-s_44628-e17dede45d7e0509540343e52fe6fce1454c5339
$bin?=\"{}\"

# NMI vector address for Ibex CPU is relative to MTVEC value: https://github.com/lowRISC/ibex/blob/97df7a5b10a1baf25633771a385aff59cea8b0fa/doc/03_reference/exception_interrupts.rst?plain=1#L57
# MTVEC is set by a bootloader and is not known upfront. Used test rom sets MTVEC to address 0x20000401 (vectored interrupt handler) and we hardcode NMI vector address below.
cpu0 NMIVectorAddress 0x2000047c
cpu0 NMIVectorLength 1

macro reset
\"\"\"
    sysbus LoadELF $bin
    sysbus LoadELF $boot
    rom_ctrl LoadVmem $boot_vmem
    otp_ctrl LoadVmem $otp_vmem
\"\"\"

runMacro $reset
"""


class OutputStyle(Enum):
    """Implemented output formats."""

    CSV = 0


class RenodeTest(object):
    """A Renode Process test, wrapping regular subprocess calls to a Renode
    process with custom writing and reading logic to facilitate the streaming
    of test logs and logical flow of an OpenTitan test. Uses two processes (a
    reader & writer) to allow test read/writes with an enforced timeout."""

    class SyncStage(Enum):
        """Tests alternate between reading from and writing to the Renode
        process. When writing, the text contents will be written and the stage
        will then progress. When reading, the stage will progress when a certain
        string appears in the output. This Enum encodes all valid states in the
        possible OpenTitan test FSMs for use in synchronisation definitions."""

        START = 0
        MONITOR_AVAILABLE = 1
        AWAITING_RESULT = 2
        FINISHED = 3

    class TestData:
        """A simple class to encapsulate the data used at a single point in
        time / phase by an OpenTitan test."""

        def __init__(
            self,
            data: Union[str, List[str]],
            req_sync_stage: "RenodeTest.SyncStage",
            next_sync_stage: "RenodeTest.SyncStage",
            sync_time_sec: float,
        ):
            """Constructor.
            Args:
                data (Union[str,List[str]]): The string/strings to write or
                read at the current phase of the test.
                req_sync_stage (RenodeTest.SyncStage): The sync stage
                required to begin this phase.
                next_sync_stage (RenodeTest.SyncStage): The sync stage
                transitioned to after completing this phase.
                sync_time_sec (float): The time in seconds to sleep after
                starting this phase, to allow synchronisation.
            """
            self.data: Union[str, List[str]] = data
            self.req_sync_stage: RenodeTest.SyncStage = req_sync_stage
            self.next_sync_stage: RenodeTest.SyncStage = next_sync_stage
            self.sync_time_sec: float = sync_time_sec
            self.synced: bool = False

        def sync(self):
            """Sleep for defined time, to allow synchronisation."""
            if self.sync_time_sec > 0:
                time.sleep(self.sync_time_sec)
            self.synced = True

        def reset_sync(self):
            """Reset the synchronisation status, to allow re-synchronisation."""
            self.synced = False

    def __init__(
        self,
        cmd: List[str],
        timeout_sec: float,
        stream_output: bool,
        read_data: List[TestData],
        write_data: List[TestData],
    ):
        """Constructor for the Renode Test

        Args:
            cmd (List[str]): The command to invoke the Renode process. A list
            of args/options.
            timeout_sec (float): Timeout in seconds for the test.
            stream_output (bool): Whether to stream test output to stdout.
            read_data (List[TestData]): The list of reader data/phases used
            in this test.
            write_data (List[TestData]): The list of writer data/phases used
            in this test.
        """
        self.cmd: List[str] = cmd
        self.timeout_sec: float = timeout_sec
        self.stream_output: bool = stream_output
        self.read_data: List[RenodeTest.TestData] = read_data
        self.write_data: List[RenodeTest.TestData] = write_data

        self._start_time: Optional[float] = None
        self._exec_time: Optional[float] = None
        self._proc: Optional[subprocess.Popen] = None

        # Index to store the current test phase of the reader/writer
        self._index = 0

        self._init_managed_vars()

    def _init_managed_vars(self) -> None:
        """Initialised the multiprocessing Manager and a small set of managed
        variables, which act as shared memory between the reading and writing
        processes.
        """
        self._manager = multiprocessing.Manager()
        self._running = self._manager.Value("running", False)
        self._sync_stage = self._manager.Value("sync_stage", RenodeTest.SyncStage.START)
        self._timed_out = self._manager.Value("timed_out", False)
        self._status_ok = self._manager.Value("status_ok", True)
        self._output = self._manager.Value("output", "")

    def _init_test_vars(self) -> None:
        """Initialises object attributes to their required states for starting
        to run the test.
        """
        self._index = 0
        self._running.value = True
        self._timed_out.value = False
        self._status_ok.value = True
        self._sync_stage.value = RenodeTest.SyncStage.START
        self._output.value = ""
        self._start_time = time.time()

    def wait_for_phase(
        self, phase_data: "RenodeTest.TestData", sleep_time: float
    ) -> bool:
        """Generic logic allowing either a reader or writer process to wait
        for the current/next phase to begin, if it has not already.

        Args:
            phase_data (RenodeTest.TestData): The current/next phase.
            sleep_time (float): The time to sleep in each busy-wait loop.

        Returns:
            bool: True if waited successfuly, False if timed out.
        """
        timeout: float = self._start_time + self.timeout_sec
        while (
            self._sync_stage.value != phase_data.req_sync_stage
            and time.time() <= timeout
        ):
            time.sleep(sleep_time)
        if time.time() >= timeout:
            self._timed_out.value = True
            self._running.value = False
            return False

        # If we just transitioned to this phase, perform the initial phase
        # synchronisation (sleep)
        if not phase_data.synced:
            phase_data.sync()
        return True

    def read_loop(self) -> None:
        """The main loop for the reader process of the Renode test. Continually
        reads in a loop, progressing through its defined test phases. At each
        test phase, it waits the defined synchronization time and then attempts
        to read one or one of several test strings from the output. When a
        string has matched, it progresses to the next phase, and awaits the next
        reading phase to begin (or timeout, or the test to finish).
        """
        while self._running.value:
            # If we've finished all read phases, finish the reader & stop.
            if self._index >= len(self.read_data):
                break

            # Wait for the current reading phase to begin, if not already
            phase_data: RenodeTest.TestData = self.read_data[self._index]
            if not self.wait_for_phase(phase_data, READ_POLL_TIME_SEC):
                break

            # Read the data from stdout for the process
            data = self._proc.stdout.read(1)
            if not data:
                self._status_ok.value = False
                self._running.value = False
                break
            self._output.value += data

            # Look for a matching suffix to end this read phase
            suffixes = phase_data.data
            if isinstance(phase_data.data, str):
                suffixes: List[str] = [phase_data.data]
            else:
                suffixes: List[str] = phase_data.data
            for suffix in suffixes:
                if self._output.value.endswith(suffix):
                    self._sync_stage.value = phase_data.next_sync_stage
                    self._index += 1
                    break

            # If streaming test output, forward each line to stdout
            end_of_test: bool = self._index >= len(self.read_data)
            if self.stream_output and (data == "\n" or end_of_test):
                line: str = self._output.value.strip().splitlines()[-1]
                print(" " * TEST_INDENTATION + line)

    def write_loop(self) -> None:
        """The main loop for the writer process of the Renode test. Continually
        writes in a loop, progressing through its defined test phases. At each
        test phase, it waits the defined synchronisation time and then writes
        its test data to stdin. When done writing, it progresses to the next
        phase, and awaits the next writing phase to begin (or timeout, or the
        test to finish).
        """
        try:

            while self._running.value:
                # If we've finished all write phases, finish the writer & stop.
                if self._index >= len(self.write_data):
                    break

                # Wait for the current writing phase to begin, if not already
                phase_data = self.write_data[self._index]
                if not self.wait_for_phase(phase_data, READ_POLL_TIME_SEC):
                    break

                # Write the data to stdin for the process and flush, and
                # progress to the next test phase.
                if isinstance(phase_data.data, str):
                    write_data: List[str] = [phase_data.data]
                else:
                    write_data: List[str] = phase_data.data
                for line in write_data:
                    self._proc.stdin.write(line)
                    self._proc.stdin.flush()
                self._sync_stage.value = phase_data.next_sync_stage
                self._index += 1

        except EOFError as e:
            print(
                f"{TEXT_COLOURS['ERROR']}EOFError in test write loop: {self.cmd}.\r\n{e}{ENDC}"
            )
            self._status_ok.value = False
            self._running.value = False

    def run(self) -> List[str]:
        """Runs the Renode test, spawning up the reader & writer processes and
        blocking until either the test finishes, or the configured timeout
        is reached.

        Returns:
            List[str]: The entire test stdin/stdout log, split by lines.
        """
        # Initialise necessary test state
        self._init_test_vars()
        self._proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            text=True,
            env=os.environ.copy(),
            preexec_fn=lambda: signal.alarm(self.timeout_sec),
        )

        # Main reader loop
        reader: multiprocessing.Process = multiprocessing.Process(target=self.read_loop)
        reader.start()

        # Main writer loop. If the timeout expires, kill the Renode process.
        try:
            self.write_loop()
        except subprocess.TimeoutExpired:
            self._timed_out.value = True
            self._running.value = False
            self._proc.kill()

        # Attempt to elegantly join the reader thread within the timeout. If
        # this fails, terminate the reader process.
        remaining_timeout: float = self.timeout_sec + self._start_time - time.time()
        if remaining_timeout < 0:
            remaining_timeout = None
        reader.join(timeout=remaining_timeout)
        if reader.is_alive():
            self._timed_out.value = True
            self._running.value = False
            reader.terminate()
        self._exec_time = time.time() - self._start_time

        return self._output.value.splitlines()

    def exec_time(self) -> Optional[float]:
        """Get the execution time for the Renode test.

        Returns:
            Optional[float]: The execution time. None if not yet been run.
        """
        return self._exec_time

    def timed_out(self) -> bool:
        """Whether the test timed out or not.

        Returns:
            bool: Whether the test timed out or not.
        """
        return self._timed_out.value

    def status_ok(self) -> bool:
        """Whether the test executed ok or not. If an error is encountered
        (e.g. the Renode process is unresponsive), this will be False.

        Returns:
            bool: Whether the test status is Ok (True) or not (False).
        """
        return self._status_ok.value


def run_test(elf: pathlib.Path, index: int, num_tests: int) -> Optional[List[str]]:
    """Run a single Renode OpenTitan Earlgrey test from an ELF file.

    Args:
        elf (pathlib.Path): The path of the ELF file to run.
        index (int): The zero-based index of this test in the list of all tests
        num_tests (int): The total number of tests being run.

    Returns:
        Optional[List[str]]: The test results in CSV format. Columns are:
        name, status, execution line, and optionally a fourth value of the
        execution log suffix if LAST_N_LINES > 0. None if the test errors.
    """
    log_prefix: str = f"[{index+1}/{num_tests}]"
    test_script_dir: str = str(TEST_SCRIPTS_DIR).removeprefix(str(SCRIPTS_DIR))
    test_script_dir = test_script_dir.removeprefix("/").removeprefix("\\")

    # Create the test .resc script for use in Renode
    try:
        script: str = TEST_SCRIPT.format(elf)
        script_name: str = elf.replace("/", "_").replace("\\", "_")
        script_name = script_name.removesuffix(".elf") + ".resc"
        script_path: str = TEST_SCRIPTS_DIR.joinpath(script_name)
        with open(script_path, "w+") as script_file:
            script_file.write(script)
    except Exception as e:
        print(
            f"{log_prefix} {TEXT_COLOURS['ERROR']}Failed to create Renode Script for ELF file: {elf}.\r\n{e}{ENDC}"
        )
        return None

    # Build the Renode commands & run the test
    try:
        renode_cmd: str = RENODE_CMD.strip().split() + RENODE_ARGS.strip().split()
        if DEBUG_LOG:
            print(
                f"{log_prefix} {TEXT_COLOURS['INFO']}Running Renode command: \"{' '.join(renode_cmd)}\"{ENDC}"
            )
        test_cmd: str = TEST_CMD.format(test_script_dir, script_name)
        if USE_TIMEOUT_OVERRIDES:
            test_file: str = str(elf).split("/")[-1].split("\\")[-1]
            timeout: float = TIMEOUT_OVERRIDES.get(
                test_file.removesuffix(".elf"),
                DEFAULT_TIMEOUT_SECS,
            )
        else:
            timeout: float = DEFAULT_TIMEOUT_SECS

        proc: RenodeTest = RenodeTest(
            renode_cmd,
            timeout,
            STREAM_TEST_OUTPUT,
            [
                # Phase 0: Read until Renode has initialized and is available
                RenodeTest.TestData(
                    RENODE_INIT_SUFFIX_STR,
                    RenodeTest.SyncStage.START,
                    RenodeTest.SyncStage.MONITOR_AVAILABLE,
                    0.1,
                ),
                # Phase 2: Look for PASS/FAIL/FAULT in the test output.
                RenodeTest.TestData(
                    list(STDOUT_STRS.values()),
                    RenodeTest.SyncStage.AWAITING_RESULT,
                    RenodeTest.SyncStage.FINISHED,
                    0,
                ),
            ],
            [
                # Phase 1: Write the command to start the test
                RenodeTest.TestData(
                    test_cmd + "\n",
                    RenodeTest.SyncStage.MONITOR_AVAILABLE,
                    RenodeTest.SyncStage.AWAITING_RESULT,
                    2.5,
                ),
            ],
        )

        # Run the Renode test and output the result.
        proc_output: List[str] = proc.run()
        # No match-case in Python < 3.10, unfortunately
        result_status: str = "UNKNOWN"
        if proc.timed_out():
            result_status = "TIMEOUT"
            if DEBUG_LOG:
                print(
                    f"{log_prefix} {TEXT_COLOURS['TIMEOUT']}Test TIMEOUT after {proc.timeout_sec} s - ELF file: {elf}{ENDC}"
                )
        elif not proc.status_ok():
            result_status = "OTHER ERROR"
            if DEBUG_LOG:
                print(
                    f"{log_prefix} {TEXT_COLOURS['FAIL']}Test OTHER ERROR - ELF file: {elf}{ENDC}"
                )
        elif proc_output[-1].endswith(STDOUT_STRS["PASS"]):
            result_status = "PASS"
            if DEBUG_LOG:
                print(
                    f"{log_prefix}  {TEXT_COLOURS['PASS']}Test PASSED - ELF file: {elf}{ENDC}"
                )
        elif proc_output[-1].endswith(STDOUT_STRS["FAIL"]):
            result_status = "FAIL"
            if DEBUG_LOG:
                print(
                    f"{log_prefix} {TEXT_COLOURS['FAIL']}Test FAILED - ELF file: {elf}{ENDC}"
                )
        elif proc_output[-1].endswith(STDOUT_STRS["FAULT"]):
            result_status = "FAULT"
            if DEBUG_LOG:
                print(
                    f"{log_prefix} {TEXT_COLOURS['FAIL']}Test FAULTED - ELF file: {elf}{ENDC}"
                )
        else:
            output_log = "\r\n".join(proc_output)
            print(
                f"{log_prefix} {TEXT_COLOURS['ERROR']}Unknown test condition. Test output log: \r\n{output_log}{ENDC}"
            )
            return None

        # Format and return the test results
        test_name: str = str(elf).split("/")[-1].split("\\")[-1]
        test_name = test_name.strip().removesuffix(".elf")
        exec_time: str = "{:.2f}".format(proc.exec_time())
        results: List[str] = [test_name, result_status, exec_time]
        if LAST_N_LINES > 0:
            log_suffix: str = "  \\n  ".join(
                line.replace(",", "")  # Assumes CSV output for now
                for line in proc_output[-LAST_N_LINES:]
            )
            results.append(log_suffix)
        return results

    except Exception as e:
        print(
            f"{log_prefix} Error when running Renode test for ELF file: {elf}.\r\n{e}{ENDC}"
        )
        return None


def run_tests(elf_patterns: List[pathlib.Path]) -> List[List[str]]:
    """Run all Renode OpenTitan Earlgrey tests that are found as ELF files
    matching any of the provided GLOB patterns.

    Args:
        elf_patterns (List[pathlib.Path]): A list of patterns to use to find
        ELF files to execute as tests, where each pattern is in GLOB format.

    Returns:
        List[List[str]]: A list of test results in CSV format (a list of
        strings). See `run_test` documentation.
    """
    # Find all ELF files matching the pattern
    elfs = []
    for elf_pattern in elf_patterns:
        elf_pattern = str(elf_pattern)
        if not elf_pattern.endswith(".elf"):
            elf_pattern += ".elf"
        elfs += glob.glob(elf_pattern)

    # Run tests on all discovered ELF files
    results = []
    for i, elf in enumerate(elfs):
        try:
            result: Optional[List[str]] = run_test(elf, i, len(elfs))
            if result:
                results.append(result)
            elif EXIT_ON_FAILURE:
                break
        except KeyboardInterrupt:
            # If the user performs a Keyboard Interrupt, stop early and output
            # all the test results collected so far.
            break
    return results


def main(
    elf_patterns: List[pathlib.Path], output: OutputStyle, to_stdout: bool = True
) -> Optional[str]:
    """Find Opentitan Earlgrey test ELF files from a given glob pattern and
    run each ELF as a Renode emulator test.

    Args:
        elf_patterns (List[pathlib.Path]): The GLOB path pattern to search for ELF files.
        output (OutputStyle): The format to output in.
        to_stdout (bool, default True): Whether to print to stdout or return a string.

    """
    # Create testing script directory if it doesn't already exist
    if not os.path.exists(TEST_SCRIPTS_DIR):
        print(
            f"{TEXT_COLOURS['INFO']}Creating missing script directory: {TEST_SCRIPTS_DIR}{ENDC}"
        )
        os.makedirs(TEST_SCRIPTS_DIR)

    # Run the tests
    results: List[List[str]] = run_tests(elf_patterns)
    headers: List[str] = ["test_name", "status", "exec_time"]
    if LAST_N_LINES > 0:
        headers.append("end_of_log")

    # Format in the requested output style
    ret: str = ""
    if output is OutputStyle.CSV:
        for line in [headers] + results:
            line_out: str = ",".join(line)
            if to_stdout:
                print(line_out)
            else:
                ret += line_out + "\r\n"

    if not to_stdout:
        return ret.strip()


if __name__ == "__main__":
    import argparse
    from collections import defaultdict

    parser = argparse.ArgumentParser(
        description="Run OpenTitan tests from their ELF files with Renode."
    )
    parser.add_argument(
        "elfs", nargs="*", help="A glob rule to match for test ELFs that should be run."
    )
    parser.add_argument(
        "-o",
        "--output",
        choices=["CSV"],
        default="CSV",
        help="The format the script should output in.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Output verbose logging messages."
    )
    parser.add_argument(
        "-s",
        "--test-output",
        action="store_true",
        help="Show test output while running.",
    )
    parser.add_argument(
        "-c",
        "--continue-on-failure",
        action="store_true",
        help="Continue running tests after any execution error, instead of immediately exiting.",
    )
    parser.add_argument(
        "-l",
        "--log-suffix-len",
        action="store",
        type=int,
        default=LAST_N_LINES,
        help="The number of lines from the end of each test output to gather. 0 disables this feature.",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        action="store",
        type=int,
        default=DEFAULT_TIMEOUT_SECS,
        help="The default timeout to use per test in seconds, unless manually overriden.",
    )
    parser.add_argument(
        "-n",
        "--no-overrides",
        action="store_true",
        help="Disable timeout overrides, forcing all tests to use the default timeout.",
    )

    # Parse command-line arguments
    args = parser.parse_args()
    DEBUG_LOG = args.verbose
    STREAM_TEST_OUTPUT = args.test_output
    EXIT_ON_FAILURE = not args.continue_on_failure
    LAST_N_LINES = args.log_suffix_len
    if LAST_N_LINES < 0:
        LAST_N_LINES = 0
    DEFAULT_TIMEOUT_SECS = args.timeout
    if DEFAULT_TIMEOUT_SECS < 0:
        DEFAULT_TIMEOUT_SECS = 0
    USE_TIMEOUT_OVERRIDES = not args.no_overrides

    elfs = [pathlib.Path(elf_path) for elf_path in args.elfs]
    output_styles = defaultdict(lambda: OutputStyle.CSV, {"CSV": OutputStyle.CSV})
    output = output_styles[args.output]

    main(elfs, output)

    sys.exit(0)
