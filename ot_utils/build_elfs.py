#!/usr/bin/env python3
# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
import pathlib
import sys
import os
import glob
import shutil
import subprocess
from parse_results import OutputStyle, main as parse_results
from typing import List

PARSE_RESULTS_SCRIPT_PATH = pathlib.Path(__file__).parent.joinpath("parse_results.py")
OUT_DIR = pathlib.Path(__file__).parent.parent.joinpath("ot_test_all")
DEFAULT_TEST_LOCATION = "//sw/device/tests"
DEBUG_LOG = False
ALLOWED_ENVS = ["fpga_cw310_rom_with_fake_keys", "fpga_cw310_test_rom", "fpga_cw310_sival"]
ELF_SUFFIX = {
    "fpga_cw310_rom_with_fake_keys": "*.elf",
    "fpga_cw310_test_rom": "*.elf",
    "fpga_cw310_sival": "*.elf",
}

# Manual override mappings to handle cases where automated test retrieval fails because
# tests have been renamed/moved etc. since they were originally run, or because they
# were originally not defined in //sw/device/tests/BUILD.bzl
OVERRIDE_MAPPINGS = {
    "alert_test": "//sw/device/tests:sensor_ctrl_alert_test",
    "chip_power_idle_load": "//sw/device/tests:chip_power_idle_load_test",
    "chip_power_sleep_load": "//sw/device/tests:chip_power_sleep_load_test",
    "hmac_functest": [
        "//sw/device/tests/crypto:hmac_functest",
        {
            "name": "//sw/device/silicon_creator/lib/drivers:hmac_functest",
            "rename": "driver_hmac_functest",
        },
    ],
    "kmac_app_rom_test": {
        "name": "//sw/device/tests:kmac_app_rom_test_fpga_cw310_rom_with_fake_keys",
        "extra_outputs": [
            "sw/device/silicon_creator/rom/mask_rom_fpga_cw310.39.scr.vmem"
        ]
    },
    "kmac_verify_functest_hardcoded": "//sw/device/tests/crypto:kmac_functest_hardcoded",
    "lc_ctrl_otp_hw_cfg_test": "//sw/device/tests:lc_ctrl_otp_hw_cfg0_test",
    "power_virus_systemtest_fpga_cw310_test_rom": "//sw/device/tests:power_virus_systemtest",
    "sha256_functest": [
        "//sw/device/tests/crypto:sha256_functest",
        "//sw/device/tests/crypto:hmac_sha256_functest",
    ],
    "sha384_functest": [
        "//sw/device/tests/crypto:sha384_functest",
        "//sw/device/tests/crypto:hmac_sha384_functest",
    ],
    "sha512_functest": [
        "//sw/device/tests/crypto:sha512_functest",
        "//sw/device/tests/crypto:hmac_sha512_functest",
    ],
    "sram_ctrl_sleep_sram_ret_contents_test": "//sw/device/tests:sram_ctrl_sleep_sram_ret_contents_no_scramble_test",
    "status_report_test_fpga_cw310_test_rom": "//sw/device/tests:status_report_test",
}
IGNORE_TESTS = set([
    "power_virus_systemtest"
])

def build_tests(ot_path: str, targets: List[str], output_missing: bool = False, cache: bool = True, mapping: bool = False, fetch_all: bool = False) -> None:
    """ TODO: docstring this function, and modularise a lot more """
    cwd = os.getcwd()
    missing = []

    # Remove repeated targets (if the same test name is used in multiple places)
    # Retain ordering whilst doing so. Also remove ignored targets.
    index = 0
    seen = set([])
    while index < len(targets):
        if targets[index] in seen or targets[index] in IGNORE_TESTS:
            targets = targets[:index] + targets[(index+1):]
        else:
            seen.add(targets[index])
        index += 1

    # Substitute the targets with manual mappings and expand to handle
    # multiple/no mappings where necessary
    index = 0
    while index < len(targets):
        test_name = targets[index].split(":")[-1]
        if test_name not in OVERRIDE_MAPPINGS:
            index += 1
            continue
        mapped = OVERRIDE_MAPPINGS[test_name]
        if isinstance(mapped, (str, dict)):
            targets[index] = mapped
            index += 1
            continue
        targets = targets[:index] + mapped + targets[(index+1):]
        index += len(mapped)

    try:
        os.chdir(ot_path)

        # Find all valid software targets to check if that option is specified.
        if fetch_all:
            command = ["./bazelisk.sh", "query", "'kind(\"._test rule\", //sw/...)'"]
            if DEBUG_LOG:
                print(f"Fetching all tests... Running command: {' '.join(command)}")
            try:
                extra_targets = subprocess.check_output(" ".join(command), shell=True, stderr=subprocess.STDOUT)
            except Exception as e:
                print(f"Error querying all Bazel test targets: {e}")
                sys.exit(1)
            for target in extra_targets.decode("utf8").strip().splitlines():
                target = target.strip()
                for env in ALLOWED_ENVS:
                    env = "_" + env
                    if target.endswith(env):
                        target_name = target.removesuffix(env)
                        if target_name not in targets and target_name.split(":")[1] not in IGNORE_TESTS:
                            targets.append(target_name)
                        break

        for i, target in enumerate(targets):
            # Renaming support for elf files of tests with the same name, in different BUILD files
            same_elf_name = True
            extra_outputs = []
            if isinstance(target, dict):
                for attr in ["name"]:
                    if attr not in target:
                        print(f"Error in OVERRIDE_MAPPINGS - target missing `{attr}` attribute: {target}")
                        sys.exit(1)
                if "rename" in target:
                    elf_name = target["rename"]
                    same_elf_name = False
                else:
                    same_elf_name = True
                if "extra_outputs" in target:
                    extra_outputs = [o for o in target["extra_outputs"]]
                target = target["name"]

            # More aggressive caching to skip all bazel queries: try all exec envs combos and check
            # for existing elf files
            target_path = target.split("//")[-1].replace(":","/")
            target_name = target_path.split("/")[-1]
            if same_elf_name:
                elf_name = target_name
            elfs = []
            if cache and not extra_outputs:
                # Limititation - we don't cache for now if we need extra test outputs. Functionality
                # to search for these additional files in our out directory (and to cache these) as
                # well needs appropriate support in this check.
                for env in ALLOWED_ENVS:
                    elfs = glob.glob(str(OUT_DIR.joinpath(f"{elf_name}_{env}*")))
                    if elfs:
                        break
                if elfs:
                    if DEBUG_LOG:
                        print((f"[{i+1}/{len(targets)}] Skipping {target} - already cached"))
                    continue

            # Get the tests / exec environments
            target_command = ["./bazelisk.sh", "query", f"'filter(\"{target}\", kind(\"._test rule\", //sw/...))'"]
            if DEBUG_LOG:
                print(f"[{i+1}/{len(targets)}] Running command: {' '.join(target_command)}")
            try:
                envs = subprocess.check_output(" ".join(target_command), shell=True, stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError:
                if output_missing:
                    missing.append(f"{target}: target not found")
                continue
            if envs.decode().strip().endswith("INFO: Empty results"):
                if output_missing:
                    missing.append(f"{target}: target not found")
                continue
            envs = [line.decode() for line in envs.splitlines()]

            # Use the first valid target found, where such a target exists
            exec_target = None
            elf_suffix = None

            # Prioritise `ALLOWED_ENVS` ordering over Bazel's ordering
            for env in ALLOWED_ENVS:
                for e_target in envs:
                    if e_target.endswith(env):
                        exec_target = e_target
                        elf_suffix = ELF_SUFFIX[env]
                        break
                if exec_target is not None:
                    break
            if exec_target is None:
                if output_missing:
                    missing.append(f"{target}: no valid exec env / target does not exist")
                continue

            if mapping:
                print(f"{exec_target}")
                continue

            # Check if the target already exists - assume no test changes and so skip
            elfs = glob.glob(str(OUT_DIR.joinpath(elf_name)) + "*.elf")
            if elfs:
                if cache and not extra_outputs:
                    # Limititation - we don't cache for now if we need extra test outputs. Functionality
                    # to search for these additional files in our out directory (and to cache these) as
                    # well needs appropriate support in this check.
                    if DEBUG_LOG:
                        print(f"[{i+1}/{len(targets)}] Skipping {target} - already cached")
                    continue
                # Remove existing ELF File so it can be replaced
                os.remove(elfs[0])

            # Build the test for the found execution environment
            target_command = ["./bazelisk.sh", "build", "--define", "bitstream=skip", exec_target]
            if DEBUG_LOG:
                print(f"[{i+1}/{len(targets)}] Running command: {' '.join(target_command)}")
            try:
                result = subprocess.check_output(target_command, stderr=subprocess.STDOUT).decode()
            except:
                # Some tests cannot be run with `--define bitstream=skip` because they require the
                # bitstream to be built. For these tests, try re-running with a bitstream on failure.
                target_command = ["./bazelisk.sh", "build", exec_target]
                if DEBUG_LOG:
                    print(f"[{i+1}/{len(targets)}] Bazel build failed; trying without skipping bitstream.")
                print(f"[{i+1}/{len(targets)}] Running command: {' '.join(target_command)}")
                result = subprocess.check_output(target_command, stderr=subprocess.STDOUT).decode()
            finally:
                if "Build completed successfully" not in result:
                    if output_missing:
                        missing.append(f"{target}: build failed ({exec_target})")
                    continue


            # Copy the ELF files to OUT_DIR
            target_path = exec_target.split("//")[-1].replace(":","/")
            target_path = target_path[:-len(env)-1] + "_prog_fpga_cw310"
            build_files = pathlib.Path("bazel-out")
            build_files = build_files.joinpath("k8-fastbuild-ST-*")
            build_files = build_files.joinpath("bin")
            elf_path = build_files.joinpath(target_path + elf_suffix)
            elfs = glob.glob(str(elf_path))
            if not elfs:
                if output_missing:
                    missing.append(f"{target}: could not find ELF file ({exec_target})")
                continue
            if DEBUG_LOG:
                print(f"[{i+1}/{len(targets)}] Found built ELF file: {elfs[0]}")
            try:
                # Delete the ELF File if it already exists
                dest_elf = [f"{elf_name}_{env}"] + pathlib.Path(elfs[0]).name.split(".")[1:]
                dest = OUT_DIR.joinpath(".".join(dest_elf))
                if os.path.exists(dest):
                    print(f"[{i+1}/{len(targets)}] Removing existing ELF file: {dest}")
                    os.remove(dest)
                # Copy the ELF file over
                print(elfs[0], dest)
                shutil.copy(elfs[0], dest)
            except Exception as e:
                print(f"Error copying ELF file: {e}")
                sys.exit(1)

            # If the Mask ROM and OTP don't already exist, copy them over as well
            """mask_rom_path = runfiles.joinpath("sw/device/silicon_creator/rom/mask_rom_fpga_cw310.elf")
            dest = OUT_DIR.joinpath("mask_rom_fpga_cw310.elf")
            try:
                if not os.path.exists(dest) and os.path.exists(mask_rom_path):
                    if DEBUG_LOG:
                        print(f"[{i+1}/{len(targets)}] `mask_rom_fpga_cw310.elf` does not exist. Copying Mask ROM.")
                    shutil.copy(mask_rom_path, OUT_DIR)c
            except Exception as e:
                print(f"Error copying Mask ROM: {e}")
                sys.exit(1)
            otp_path = pathlib.Path("bazel-bin/hw/ip/otp_ctrl/data/img_rma.24.vmem")
            dest = OUT_DIR.joinpath("img_rma.24.vmem")
            try:
                if not os.path.exists(dest) and os.path.exists(otp_path):
                    if DEBUG_LOG:
                        print(f"[{i+1}/{len(targets)}] `img_rma.24.vmem` does not exist. Copying OTP.")
                    shutil.copy(otp_path, OUT_DIR)
            except Exception as e:
                print(f"Error copying OTP: {e}")
                sys.exit(1)"""

            # Copy any other extra output files specified by the target as well
            runfiles = pathlib.Path("bazel-bin").joinpath(target_path + ".bash.runfiles")
            runfiles = runfiles.joinpath("_main")
            for output in extra_outputs:
                output_path = runfiles.joinpath(output)
                output_name = pathlib.Path(output).name
                dest = OUT_DIR.joinpath(output_name)
                try:
                    if not os.path.exists(dest) and os.path.exists(output_path):
                        if DEBUG_LOG:
                            print(f"[{i+1}/{len(targets)}] `{output_name}` does not exist. Copying it over now.")
                        shutil.copy(output_path, dest)
                except Exception as e:
                    print(f"Error copying output `{output_name}`: {e}")
                    sys.exit(1)

        os.chdir(cwd)
    except Exception as e:
        print(f"An error occured when trying to run Bazel: {e}")
        os.chdir(cwd)
        sys.exit(1)

    for entry in missing:
        print(entry)

def main(ot_path: pathlib.Path, fpath: pathlib.Path, output_missing: bool = False, cache: bool = True, mapping: bool = False, fetch_all: bool = False) -> None:
    """ Parses a QEMU OpenTitan Earlgrey test results output file and transforms
    the data into a specified format which is then output. Directly prints to
    stdout and so does not return anything.

    Args:
        ot_path (pathlib.Path): The path to the root directory of OpenTitan.
        fpath (pathlib.Path): The path to the file to read the contents of.
        output_missing (bool, default False): Whether to output missing tests.
        cache (bool, default False): Whether to allow caching (do not rebuild
        ELF files that already exist).
        mapping (bool, default False): Whether to just output the computed
        test mappings, and do nothing else
    """
    # Create output directory if it doesn't already exist
    if not os.path.exists(OUT_DIR):
        os.makedirs(OUT_DIR)

    # Retrieve test targets from past results file.
    results = parse_results(fpath, OutputStyle.CSV, to_stdout=False).strip()
    test_names = [line.split(",")[0] for line in results.splitlines()][1:]
    #targets = [f"{DEFAULT_TEST_LOCATION}:{name}" for name in test_names]

    # Move to the OpenTitan directory and build the tests
    build_tests(ot_path, test_names, output_missing, cache, mapping, fetch_all)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Builds ELF files using Bazel for a specified set of Opentitan tests'
    )
    parser.add_argument('ot_path', help="The path to the root directory of OpenTitan to use.")
    parser.add_argument('filename', help="The path to the file containing the old test results.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Output verbose logging messages.")
    parser.add_argument("-m", "--missing", action="store_true", help="Whether to output missing tests (true) or not.")
    parser.add_argument("-M", "--mapping", action="store_true", help="Enable to just output the mappings used, and do nothing else.")
    parser.add_argument("-c", "--no_cache", action="store_true", help="If true, will rebuild any existing ELF files.")
    parser.add_argument("-a", "--fetch-all", action="store_true", help="As well as the past results, fetch *all* ELF files that can be found.")

    # Parse command-line arguments
    args = parser.parse_args()
    ot_path = pathlib.Path(args.ot_path)
    fpath = pathlib.Path(args.filename)
    DEBUG_LOG = args.verbose

    main(ot_path, fpath, args.missing, not args.no_cache, args.mapping, args.fetch_all)

    sys.exit(0)
