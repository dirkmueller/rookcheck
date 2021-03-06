# Copyright (c) 2019 SUSE LINUX GmbH
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import re
import time
import wget

from tests.lib.common import execute
from tests.lib import common
from tests.lib.rook.base import RookBase

logger = logging.getLogger(__name__)


class RookCluster(RookBase):
    def __init__(self, workspace, kubernetes):
        super().__init__(workspace, kubernetes)
        self._rook_built = False
        self.builddir = os.path.join(self.workspace.working_dir, 'rook_build')
        os.mkdir(self.builddir)
        self.go_tmpdir = os.path.join(self.workspace.working_dir, 'tmp')
        os.mkdir(self.go_tmpdir)

    def build_rook(self):
        logger.info("[build_rook] Download go")
        wget.download(
            "https://dl.google.com/go/go1.13.9.linux-amd64.tar.gz",
            os.path.join(self.builddir, 'go-amd64.tar.gz'),
            bar=None,
        )

        logger.info("[build_rook] Unpack go")
        execute(
            "tar -C %s -xzf %s"
            % (self.builddir, os.path.join(self.builddir, 'go-amd64.tar.gz'))
        )

        # TODO(jhesketh): Allow setting rook version
        logger.info("[build_rook] Checkout rook")
        execute(
            "mkdir -p %s"
            % os.path.join(self.builddir, 'src/github.com/rook/rook')
        )
        execute(
            "git clone https://github.com/rook/rook.git %s"
            % os.path.join(self.builddir, 'src/github.com/rook/rook'),
            log_stderr=False
        )
        # TODO(jhesketh): Allow testing various versions of rook
        execute(
            "cd %s && git checkout v1.3.1"
            % os.path.join(self.builddir, 'src/github.com/rook/rook'),
            log_stderr=False
        )

        logger.info("[build_rook] Make rook")
        execute(
            "PATH={builddir}/go/bin:$PATH GOPATH={builddir} "
            "TMPDIR={tmpdir} "
            "make --directory='{builddir}/src/github.com/rook/rook' "
            "-j BUILD_REGISTRY='rook-build' IMAGES='ceph' "
            "build".format(builddir=self.builddir,
                           tmpdir=self.go_tmpdir),
            log_stderr=False,
            logger_name="make -j BUILD_REGISTRY='rook-build' IMAGES='ceph'",
        )

        logger.info("[build_rook] Tag image")
        execute('docker tag "rook-build/ceph-amd64" rook/ceph:master')

        logger.info("[build_rook] Save image tar")
        # TODO(jhesketh): build arch may differ
        execute(
            "docker save rook/ceph:master | gzip > %s"
            % os.path.join(self.builddir, 'rook-ceph.tar.gz')
        )

        self.ceph_dir = os.path.join(
            self.builddir,
            'src/github.com/rook/rook/cluster/examples/kubernetes/ceph'
        )

        self._rook_built = True

    def upload_rook_image(self):
        self.kubernetes.hardware.ansible_run_playbook("playbook_rook.yaml")

    # TODO (bleon)
    # This method should be replaced inf favor of using base install() one
    def install_rook(self):
        if not self._rook_built:
            raise Exception("Rook must be built before being installed")
        # TODO(jhesketh): We may want to provide ways for tests to override
        #                 these
        logger.info("Applying common.yaml and operator.yaml")
        self.kubernetes.kubectl_apply(
            os.path.join(self.ceph_dir, 'common.yaml'))
        self.kubernetes.kubectl_apply(
            os.path.join(self.ceph_dir, 'operator.yaml'))

        # TODO(jhesketh): Check if sleeping is necessary
        time.sleep(10)

        logger.info("Applying cluster.yaml and toolbox.yaml")
        self.kubernetes.kubectl_apply(
            os.path.join(self.ceph_dir, 'cluster.yaml'))
        self.kubernetes.kubectl_apply(
            os.path.join(self.ceph_dir, 'toolbox.yaml'))

        time.sleep(10)

        self.kubernetes.kubectl_apply(
            os.path.join(self.ceph_dir, 'csi/rbd/storageclass.yaml'))

        logger.info("Wait for OSD prepare to complete "
                    "(this may take a while...)")
        pattern = re.compile(r'.*rook-ceph-osd-prepare.*Completed')

        common.wait_for_result(
            self.kubernetes.kubectl, "--namespace rook-ceph get pods",
            log_stdout=False,
            matcher=common.regex_count_matcher(pattern, 3),
            attempts=90, interval=10)

        # TODO (bleon)
        # this should be enough to a rook deployment to be ready
        # FS/RBD are not needed to HEALTH_OK

        self.kubernetes.kubectl_apply(
            os.path.join(self.ceph_dir, 'filesystem.yaml'))

        logger.info("Wait for 2 mdses to start")
        pattern = re.compile(r'.*rook-ceph-mds-myfs.*Running')

        common.wait_for_result(
            self.kubernetes.kubectl, "--namespace rook-ceph get pods",
            log_stdout=False,
            matcher=common.regex_count_matcher(pattern, 2),
            attempts=20, interval=5)

        logger.info("Wait for myfs to be active")
        pattern = re.compile(r'.*active')

        common.wait_for_result(
            self.execute_in_ceph_toolbox, "ceph fs status myfs",
            log_stdout=False,
            matcher=common.regex_matcher(pattern),
            attempts=20, interval=5)

        logger.info("Rook successfully installed and ready!")

    def execute_in_ceph_toolbox(self, command, log_stdout=False):
        if not self.toolbox_pod:
            self.toolbox_pod = self.kubernetes.get_pod_by_app_label(
                "rook-ceph-tools")

        return self.kubernetes.execute_in_pod(
            command, self.toolbox_pod, log_stdout=False)
