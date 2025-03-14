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
from typing import List, Any, Optional, Tuple
from enum import Enum

class OutputStyle(Enum):
    CSV = 0

SCRIPTS_DIR = pathlib.Path(__file__).parent.parent.joinpath("scripts")
TIMEOUT_SECS = 60
TEST_SCRIPTS_DIR = SCRIPTS_DIR.joinpath("ot_tests")
RENODE_CMD = "./renode"
RENODE_ARGS = "--console --disable-gui --plain"
TEST_CMD = "start @scripts/{}/{}"
PASS_STDOUT = "PASS"
FAIL_STDOUT = "FAIL"
FAULT_STDOUT = "FAULT"
DEBUG_LOG = False
STREAM_TEST_OUTPUT = False
TEST_INDENTATION = 2
LAST_N_LINES = 4

INFO_BLUE = '\033[94m'
PASS_GREEN = '\033[92m'
FAIL_RED = '\033[91m'
TIMEOUT_ORANGE = '\033[93m'
ENDC = '\033[0m'

TIMEOUT_OVERRIDES = {
    "mod_exp_otbn_functest_wycheproof_fpga_cw310_test_rom": 30000,
    "mod_exp_ibex_functest_wycheproof_fpga_cw310_test_rom": 30000,
    "rsa_3072_verify_functest_wycheproof_fpga_cw310_test_rom": 30000,
    "retention_sram_functest_fpga_cw310_test_rom": 30000,

}

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

class RenodeProc(object):
    
    class SyncStage(Enum):
        START = 0
        MONITOR_AVAILABLE = 1
        AWAITING_RESULT = 2
        FINISHED = 3

    class TestData:

        def __init__(self, data: Any, req_sync_stage: "RenodeProc.SyncStage", next_sync_stage: "RenodeProc.SyncStage", sync_time_sec: float):
            self.data = data
            self.req_sync_stage = req_sync_stage
            self.next_sync_stage = next_sync_stage
            self.sync_time_sec = sync_time_sec
            self.synced = False
        
        def sync(self):
            if self.sync_time_sec > 0:
                time.sleep(self.sync_time_sec)
            self.synced = True

    def __init__(self, cmd: List[str], timeout_sec: int, stream_output: bool, read_data: List[TestData], write_data: List[TestData]):
        self.cmd = cmd
        self.timeout_sec = timeout_sec
        self.start = 0
        self.exec_time = None
        self.stream_output = stream_output
        self.read_data = read_data
        self.write_data = write_data
        self.read_index = 0
        self.write_index = 0
        self.proc = None
        self.manager = multiprocessing.Manager()
        self.running = self.manager.Value("running", False)
        self.sync_stage = self.manager.Value("sync_stage", RenodeProc.SyncStage.START)
        self.timed_out = self.manager.Value("timed_out", False)
        self.status_ok = self.manager.Value("status_ok", True)
        self.output = self.manager.Value("output", "")
    
    def read_loop(self) -> None:
        while self.running.value:
            if self.read_index >= len(self.read_data):
                break
            test_data = self.read_data[self.read_index]
            #print("READ", time.time(), self.sync_stage.value, test_data.req_sync_stage, test_data.data, test_data.next_sync_stage)
            while self.sync_stage.value != test_data.req_sync_stage and time.time() <= (self.start + self.timeout_sec):
                time.sleep(0.1)
            if time.time() >= (self.start + self.timeout_sec):
                self.timed_out.value = True
                self.running.value = False
                break
            if not test_data.synced:
                test_data.sync()
            data = self.proc.stdout.read(1)
            if not data:
                self.status_ok.value = False
                self.running.value = False
                break
            self.output.value += data
            if isinstance(test_data.data, str):
                suffixes = [test_data.data]
            else:
                suffixes = test_data.data
            for suffix in suffixes:
                if self.output.value.endswith(suffix):  # TODO not a suffix at the moment
                    self.sync_stage.value = test_data.next_sync_stage
                    self.read_index += 1
                    break
            if self.stream_output and (data == "\n" or self.read_index >= len(self.read_data)):
                print(" " * TEST_INDENTATION + self.output.value.strip().splitlines()[-1])

    def write_loop(self) -> bool:
        try:
            while self.running.value:
                if self.write_index >= len(self.write_data):
                    break
                test_data = self.write_data[self.write_index]
                #print("WRITE", time.time(), self.sync_stage.value, test_data.req_sync_stage, test_data.data, test_data.next_sync_stage)
                while self.sync_stage.value != test_data.req_sync_stage and time.time() <= (self.start + self.timeout_sec):
                    time.sleep(0.1)
                if time.time() >= (self.start + self.timeout_sec):
                    self.timed_out.value = True
                    self.running.value = False
                    break
                if not test_data.synced:
                    test_data.sync()
                if not test_data.data:
                    self.status_ok.value = False
                    self.running.value = False
                    return False
                self.proc.stdin.write(test_data.data)
                self.proc.stdin.flush()
                self.sync_stage.value = test_data.next_sync_stage
                self.write_index += 1
        except EOFError:
            self.status_ok.value = False
            self.running.value = False
            return False
        return True

    def run(self) -> List[str]:
        self.read_index = 0
        self.write_index = 0
        self.running.value = True
        self.timed_out.value = False
        self.status_ok.value = True
        self.sync_stage.value = RenodeProc.SyncStage.START
        self.output.value = ""
        self.start = time.time()

        env = os.environ.copy()
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, text=True, env=env, preexec_fn=lambda: signal.alarm(self.timeout_sec))

        reader = multiprocessing.Process(target=self.read_loop)
        reader.start()

        try:
            self.status_ok.value |= self.write_loop()
        except subprocess.TimeoutExpired:
            self.timed_out.value = True
            self.running.value = False
            self.proc.kill()

        to = self.timeout_sec + self.start - time.time()
        if to < 0:
            to = None
        reader.join(timeout=to)
        if reader.is_alive():
            self.timed_out.value = True
            self.running.value = False
            reader.terminate()
        self.exec_time = time.time() - self.start

        return self.output.value.splitlines()


def run_tests(elf_pattern: pathlib.Path) -> List[Tuple[str]]:
    # Find ELF files matching the pattern
    elf_pattern = str(elf_pattern)
    if not elf_pattern.endswith(".elf"):
        elf_pattern += ".elf"
    elfs = glob.glob(elf_pattern)

    # Determine the test script location for forming commands prior to running
    # any tests.
    test_script_dir = str(TEST_SCRIPTS_DIR).removeprefix(str(SCRIPTS_DIR))
    test_script_dir = test_script_dir.removeprefix("/").removeprefix("\\")

    # Test all the discovered ELF files
    results = []
    for i, elf in enumerate(elfs):
        try:

            # Create the test script for use in Renode
            try:
                script = TEST_SCRIPT.format(elf)
                script_name = elf.replace("/", "_").replace("\\","_").removesuffix(".elf") + ".resc"
                script_path = TEST_SCRIPTS_DIR.joinpath(script_name)
                with open(script_path, "w+") as f:
                    f.write(script) 
            except Exception as e:
                print(f"[{i+1}/{len(elfs)}] {FAIL_RED}Failed to create Renode Script for ELF file: {elf}.\r\n{e}{ENDC}")
                return results

            # Construct Renode commands and run the test as a subprocess
            try:
                renode_cmd = RENODE_CMD.strip().split(" ") + RENODE_ARGS.strip().split(" ")
                if DEBUG_LOG:
                    print(f"[{i+1}/{len(elfs)}] {INFO_BLUE}Running Renode command: \"{' '.join(renode_cmd)}\"{ENDC}")
                test_cmd = TEST_CMD.format(test_script_dir, script_name)
                timeout = TIMEOUT_OVERRIDES.get(str(elf).split("/")[-1].split("\\")[-1].removesuffix(".elf"), TIMEOUT_SECS)
                proc = RenodeProc(
                    renode_cmd,
                    timeout,
                    STREAM_TEST_OUTPUT,
                    [
                        RenodeProc.TestData(")\n\n", RenodeProc.SyncStage.START, RenodeProc.SyncStage.MONITOR_AVAILABLE, 0.1),
                        RenodeProc.TestData([PASS_STDOUT, FAIL_STDOUT, FAULT_STDOUT], RenodeProc.SyncStage.AWAITING_RESULT, RenodeProc.SyncStage.FINISHED, 0),
                    ],
                    [
                        RenodeProc.TestData(test_cmd + "\n", RenodeProc.SyncStage.MONITOR_AVAILABLE, RenodeProc.SyncStage.AWAITING_RESULT, 2.5),
                    ],
                )
                proc_output = proc.run()
                result_status = "UNKNOWN"
                if proc.timed_out.value:
                    result_status = "TIMEOUT"
                    if DEBUG_LOG:
                        print(f"[{i+1}/{len(elfs)}] {TIMEOUT_ORANGE}Test TIMEOUT after {proc.timeout_sec} s - ELF file: {elf}{ENDC}")
                elif not proc.status_ok.value:
                    result_status = "OTHER ERROR"
                    if DEBUG_LOG:
                        print(f"[{i+1}/{len(elfs)}] {FAIL_RED}Test OTHER ERROR - ELF file: {elf}{ENDC}")
                elif proc_output[-1].endswith(PASS_STDOUT):
                    result_status = "PASS"
                    if DEBUG_LOG:
                        print(f"[{i+1}/{len(elfs)}] {PASS_GREEN}Test PASSED - ELF file: {elf}{ENDC}")
                elif proc_output[-1].endswith(FAIL_STDOUT):
                    result_status = "FAIL"
                    if DEBUG_LOG:
                        print(f"[{i+1}/{len(elfs)}] {FAIL_RED}Test FAILED - ELF file: {elf}{ENDC}")
                elif proc_output[-1].endswith(FAULT_STDOUT):
                    result_status = "FAULT"
                    if DEBUG_LOG:
                        print(f"[{i+1}/{len(elfs)}] {FAIL_RED}Test FAULTED - ELF file: {elf}{ENDC}")
                else:
                    output_log = "\r\n".join(proc_output)
                    print(f"[{i+1}/{len(elfs)}] {FAIL_RED}Unknown test condition. Test output log: \r\n{output_log}{ENDC}")
                    break
                test_name = str(elf).split("/")[-1].split("\\")[-1].strip().removesuffix(".elf")
                if LAST_N_LINES > 0:
                    results.append((test_name, result_status, "{:2f}".format(proc.exec_time), "  \\n  ".join(line.replace(",", "") for line in proc_output[-LAST_N_LINES:])))
                else:
                    results.append((test_name, result_status, "{:2f}".format(proc.exec_time)))
            except Exception as e:
                print(f"[{i+1}/{len(elfs)}] Error when running Renode test for ELF file: {elf}.\r\n{e}{ENDC}")
                return results
        
        except KeyboardInterrupt:
            break
    return results


def main(elf_pattern: pathlib.Path, output: OutputStyle, to_stdout: bool = True) -> Optional[str]:
    """ Find Opentitan Earlgrey test ELF files from a given glob pattern and
    run each ELF as a Renode emulator test.

    Args:
        elf_pattern (pathlib.Path): The GLOB path pattern to search for ELF files.
        output (OutputStyle): The format to output in.
        to_stdout (bool, default True): Whether to print to stdout or return a string.
        
    """
    # Create testing script directory if it doesn't already exist
    if not os.path.exists(TEST_SCRIPTS_DIR):
        print(f"{INFO_BLUE}Creating missing script directory: {TEST_SCRIPTS_DIR}{ENDC}")
        os.makedirs(TEST_SCRIPTS_DIR)

    results = run_tests(elf_pattern)
    headers = ["test_name", "status", "exec_time"]
    if LAST_N_LINES > 0:
        headers.append("end_of_log")

    # Format in the requested output style
    ret = ""
    if output is OutputStyle.CSV:
        if to_stdout:
            print(",".join(headers))
        else:
            ret += ",".join(headers) + "\r\n"
        for result in results:
            if to_stdout:
                print(",".join(result))
            else:
                ret += ",".join(result) + "\r\n"
    
    # Return the output if not printing directly to stdout
    if not to_stdout:
        while ret.endswith("\r\n"):
            ret = ret[:-2]
        return ret


if __name__ == "__main__":
    import argparse
    from collections import defaultdict

    parser = argparse.ArgumentParser(
        description='Run OpenTitan tests from their ELF files with Renode.'
    )
    parser.add_argument('elfs', help="A glob rule to match for test ELFs that should be run.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Output verbose logging messages.")
    parser.add_argument("-t", "--test_output", action="store_true", help="Show test output while running.")
    parser.add_argument('output', choices=["CSV"], help="The format the script should output in.")

    # Parse command-line arguments
    args = parser.parse_args()
    elfs = pathlib.Path(args.elfs)
    DEBUG_LOG = args.verbose
    STREAM_TEST_OUTPUT = args.test_output
    output_styles = defaultdict(lambda: OutputStyle.CSV, {"CSV": OutputStyle.CSV})
    output = output_styles[args.output]

    main(elfs, output)

    sys.exit(0)
