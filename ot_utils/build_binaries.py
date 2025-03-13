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

PARSE_RESULTS_SCRIPT_PATH = pathlib.Path(__file__).parent.joinpath("parse_results.py")
OUT_DIR = pathlib.Path(__file__).parent.parent.joinpath("ot_test")
DEFAULT_TEST_LOCATION = "//sw/device/tests"
DEBUG_LOG = False
ALLOWED_ENVS = ["fpga_cw310_rom_with_fake_keys", "fpga_cw310_test_rom", "fpga_cw310_sival"]
BINARY_SUFFIX = {
    "fpga_cw310_rom_with_fake_keys": ".*.signed.bin",
    "fpga_cw310_test_rom": ".bin",
    "fpga_cw310_sival": ".*.signed.bin",
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

def build_tests(ot_path: str, targets: list[str], output_missing: bool = False, cache: bool = True, mapping: bool = False, fetch_all: bool = False) -> None:
    """ TODO: docstring this function, and modularise a lot more """
    cwd = os.getcwd()
    missing = []

    # Remove repeated targets (if the same test name is used in multiple places)
    # Retain ordering whilst doing so
    index = 0
    seen = set([])
    while index < len(targets):
        if targets[index] in seen:
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
                        if target_name not in targets:
                            targets.append(target_name)
                        break

        for i, target in enumerate(targets):
            # Renaming support for binaries of tests with the same name, in different BUILD files
            same_bin_name = True
            extra_outputs = []
            if isinstance(target, dict):
                for attr in ["name"]:
                    if attr not in target:
                        print(f"Error in OVERRIDE_MAPPINGS - target missing `{attr}` attribute: {target}")
                        sys.exit(1)
                if "rename" in target:
                    bin_name = target["rename"]
                    same_bin_name = False
                else:
                    same_bin_name = True
                if "extra_outputs" in target:
                    extra_outputs = [o for o in target["extra_outputs"]]
                target = target["name"]

            # More aggressive caching to skip all bazel queries: try all exec envs combos and check
            # for existing signed binaries
            target_path = target.split("//")[-1].replace(":","/")
            target_name = target_path.split("/")[-1]
            if same_bin_name:
                bin_name = target_name
            binaries = []
            if cache and not extra_outputs:
                # Limititation - we don't cache for now if we need extra test outputs. Functionality
                # to search for these additional files in our out directory (and to cache these) as
                # well needs appropriate support in this check.
                for env in ALLOWED_ENVS:
                    binaries = glob.glob(str(OUT_DIR.joinpath(f"{bin_name}_{env}*")))
                    if binaries:
                        break
                if binaries:
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
            binary_suffix = None

            # Prioritise `ALLOWED_ENVS` ordering over Bazel's ordering
            for env in ALLOWED_ENVS:
                for target in envs:
                    if target.endswith(env):
                        exec_target = target
                        binary_suffix = BINARY_SUFFIX[env]
                        break
                if exec_target is not None:
                    break
            if exec_target is None:
                if output_missing:
                    missing.append(f"{target}: no valid exec env")
                continue

            if mapping:
                print(f"{exec_target}")
                continue

            # Check if the target already exists - assume no test changes and so skip
            binaries = glob.glob(str(OUT_DIR.joinpath(bin_name)) + "*.bin")
            if binaries:
                if cache and not extra_outputs:
                    # Limititation - we don't cache for now if we need extra test outputs. Functionality
                    # to search for these additional files in our out directory (and to cache these) as
                    # well needs appropriate support in this check.
                    if DEBUG_LOG:
                        print(f"[{i+1}/{len(targets)}] Skipping {target} - already cached")
                    continue
                # Remove existing binary so it can be replaced
                os.remove(binaries[0])

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


            # Copy the binaries to OUT_DIR
            target_path = exec_target.split("//")[-1].replace(":","/")
            runfiles = pathlib.Path("bazel-bin").joinpath(target_path + ".bash.runfiles")
            runfiles = runfiles.joinpath("_main")
            signed_bin_path = runfiles.joinpath(target_path + binary_suffix)
            binaries = glob.glob(str(signed_bin_path))
            if not binaries:
                if output_missing:
                    missing.append(f"{target}: could not find binaries ({exec_target})")
                continue
            if DEBUG_LOG:
                print(f"[{i+1}/{len(targets)}] Found built binary: {binaries[0]}")
            try:
                # Delete the binary if it already exists
                dest_bin = [f"{bin_name}_{env}"] + pathlib.Path(binaries[0]).name.split(".")[1:]
                dest = OUT_DIR.joinpath(".".join(dest_bin))
                if os.path.exists(dest):
                    print(f"[{i+1}/{len(targets)}] Removing existing binary: {dest}")
                    os.remove(dest)
                # Copy the binary over
                shutil.copy(binaries[0], dest)
            except Exception as e:
                print(f"Error copying signed binary: {e}")
                sys.exit(1)

            # If the Mask ROM and OTP don't already exist, copy them over as well
            mask_rom_path = runfiles.joinpath("sw/device/silicon_creator/rom/mask_rom_fpga_cw310.elf")
            dest = OUT_DIR.joinpath("mask_rom_fpga_cw310.elf")
            try:
                if not os.path.exists(dest) and os.path.exists(mask_rom_path):
                    if DEBUG_LOG:
                        print(f"[{i+1}/{len(targets)}] `mask_rom_fpga_cw310.elf` does not exist. Copying Mask ROM.")
                    shutil.copy(mask_rom_path, OUT_DIR)
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
                sys.exit(1)

            # Copy any other extra output files specified by the target as well
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
        binaries that already exist).
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
        description='Builds binaries using Bazel for a specified set of Opentitan tests'
    )
    parser.add_argument('ot_path', help="The path to the root directory of OpenTitan to use.")
    parser.add_argument('filename', help="The path to the file containing the old test results.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Output verbose logging messages.")
    parser.add_argument("-m", "--missing", action="store_true", help="Whether to output missing tests (true) or not.")
    parser.add_argument("-M", "--mapping", action="store_true", help="Enable to just output the mappings used, and do nothing else.")
    parser.add_argument("-c", "--no_cache", action="store_true", help="If true, will rebuild any existing binaries.")
    parser.add_argument("-a", "--fetch-all", action="store_true", help="As well as the past results, fetch *all* binaries that can be found.")

    # Parse command-line arguments
    args = parser.parse_args()
    ot_path = pathlib.Path(args.ot_path)
    fpath = pathlib.Path(args.filename)
    DEBUG_LOG = args.verbose

    main(ot_path, fpath, args.missing, not args.no_cache, args.mapping, args.fetch_all)

    sys.exit(0)
