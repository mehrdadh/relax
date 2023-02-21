# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=invalid-name, wrong-import-position
"""Utility functions for finding information about current device."""
import os
import re
import sys
import tvm
import psutil
import subprocess
import shutil
import tvm


def get_llvm_target() -> tvm.target.Target:
    """Extract fully featured llvm target for current device.

    Returns
    -------
    target : tvm.target.Target
        A TVM target that fully describes the current devices CPU.
    """
    # If we cant find llc, we wont be able to extract more information.
    if shutil.which("llc") is None:
        return "llvm"

    # Get host information from llc
    stream = subprocess.run(["llc", "--version"], stdout=subprocess.PIPE)
    cpu_info = stream.stdout.decode("utf-8")

    # Parse out cpu line
    cpu = re.search("(?<=Host CPU: ).+", cpu_info).group(0)

    # Next extract attribute string.
    platform = sys.platform
    # Linux
    if platform == "linux" or platform == "linux2":
        stream = subprocess.run("lscpu", stdout=subprocess.PIPE)
        feature_info = stream.stdout.decode("utf-8")
        features = re.search("(?<=Flags: ).+", feature_info).group(0)
        features = features.lower().strip().split(" ")
        march = re.search("(?<=Architecture: ).+", feature_info).group(0).strip()
        cores = re.search("(?<=Core\(s\) per socket: ).+", feature_info).group(0).strip()
        sockets = re.search("(?<=Socket\(s\): ).+", feature_info).group(0).strip()
        total_cores = str(int(cores) * int(sockets))
        # Special case for x86_64 mismatch between underscore and hyphen
        if march == "x86_64":
            march = "x86-64"
    else:
        raise ValueError("Platform %s is not supported." % platform)

    # Now we'll extract the architecture of the target.
    stream = subprocess.run(["llc", "--version"], stdout=subprocess.PIPE)
    march_info = stream.stdout.decode("utf-8")
    # Remove header.
    march_options = re.search("(?<=Registered Targets:).*", march_info, re.DOTALL).group(0)
    march_list = []
    for march_line in march_options.split("\n"):
        if march_line != "":
            march_list.append(march_line.strip().split(" ")[0])

    valid_march = False
    if march in march_list:
        valid_march = True

    # Build the base target.
    host_target = (
        subprocess.run(["llvm-config", "--host-target"], stdout=subprocess.PIPE)
        .stdout.decode("utf-8")
        .strip("\n")
    )
    target = "llvm -mcpu=%s -mtriple=%s -num_cores=%s" % (cpu, host_target, total_cores)

    # If possible, add more attribute information.
    if valid_march:
        # Get list of valid attributes for the target architecture.
        sp = subprocess.run(["llc", "-march=%s" % march, "-mattr=help"], stderr=subprocess.PIPE)
        attrs_info = sp.stderr.decode("utf-8")
        supported_attrs = re.search(
            r"(?<=Available features for this target:).*(?=Use \+feature to enable a feature)",
            attrs_info,
            re.DOTALL,
        ).group(0)
        attrs_list = []
        for attrs_line in supported_attrs.split("\n"):
            if attrs_line != "":
                attrs_list.append(attrs_line.strip().split(" ")[0])

        attrs = []
        # Find which features are supported attrs.
        for f in features:
            if f in attrs_list:
                attrs.append(f)

        # Compuse attributes into valid string.
        attrs_string = ""
        for attr in attrs:
            attrs_string += "+%s," % attr
        # Remove final comma
        attrs_string = attrs_string[:-1]

        # Now we can add more information to the llvm target.
        target = "%s -mattr=%s" % (target, attrs_string)

    return target


def get_default_target(ctx: str) -> str:
    if ctx == "cpu":
        target = get_llvm_target()
    elif ctx == "gpu":
        if tvm.gpu(0).exist:
            target = "cuda"
        elif tvm.rocm(0).exist:
            target = "rocm"
        else:
            raise NotImplementedError("Only cuda and rocm GPUs currently supported.")
    else:
        raise NotImplementedError(
            "Context %s does not have a clear target. Please specify one explicitly" % target
        )

    return target


def get_default_threads() -> int:
    n = os.environ.get("TVM_NUM_THREADS")
    if n is not None:
        return int(n)
    return psutil.cpu_count()