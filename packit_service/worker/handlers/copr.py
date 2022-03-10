# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import logging
from datetime import datetime, timezone
from typing import Optional

from celery import signature
from ogr.services.github import GithubProject
from ogr.services.gitlab import GitlabProject
from packit.config import (
    JobConfig,
    JobType,
)
from packit.config import JobConfigTriggerType
from packit.config.package_config import PackageConfig

from packit_service.constants import (
    COPR_API_SUCC_STATE,
    COPR_SRPM_CHROOT,
    PG_BUILD_STATUS_FAILURE,
    PG_BUILD_STATUS_SUCCESS,
)
from packit_service.models import (
    AbstractTriggerDbType,
    CoprBuildTargetModel,
    SRPMBuildModel,
)
from packit_service.worker.events import (
    CoprBuildEndEvent,
    AbstractCoprBuildEvent,
    CoprBuildStartEvent,
    MergeRequestGitlabEvent,
    PullRequestGithubEvent,
    PushGitHubEvent,
    PushGitlabEvent,
    PushPagureEvent,
    ReleaseEvent,
    CheckRerunCommitEvent,
    CheckRerunPullRequestEvent,
    CheckRerunReleaseEvent,
    AbstractPRCommentEvent,
)
from packit_service.service.urls import get_copr_build_info_url, get_srpm_build_info_url
from packit_service.utils import dump_job_config, dump_package_config
from packit_service.worker.build import CoprBuildJobHelper
from packit_service.worker.handlers.abstract import (
    JobHandler,
    TaskName,
    configured_as,
    reacts_to,
    required_for,
    run_for_comment,
    run_for_check_rerun,
)
from packit_service.worker.monitoring import measure_time
from packit_service.worker.reporting import BaseCommitStatus
from packit_service.worker.result import TaskResults

logger = logging.getLogger(__name__)


@configured_as(job_type=JobType.copr_build)
@configured_as(job_type=JobType.build)
@run_for_comment(command="build")
@run_for_comment(command="copr-build")
@run_for_comment(command="rebuild-failed")
@run_for_check_rerun(prefix="rpm-build")
@reacts_to(ReleaseEvent)
@reacts_to(PullRequestGithubEvent)
@reacts_to(PushGitHubEvent)
@reacts_to(PushGitlabEvent)
@reacts_to(MergeRequestGitlabEvent)
@reacts_to(AbstractPRCommentEvent)
@reacts_to(CheckRerunPullRequestEvent)
@reacts_to(CheckRerunCommitEvent)
@reacts_to(CheckRerunReleaseEvent)
class CoprBuildHandler(JobHandler):
    task_name = TaskName.copr_build

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        event: dict,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            event=event,
        )

        self._copr_build_helper: Optional[CoprBuildJobHelper] = None

    @property
    def copr_build_helper(self) -> CoprBuildJobHelper:
        if not self._copr_build_helper:
            self._copr_build_helper = CoprBuildJobHelper(
                service_config=self.service_config,
                package_config=self.package_config,
                project=self.project,
                metadata=self.data,
                db_trigger=self.data.db_trigger,
                job_config=self.job_config,
                build_targets_override=self.data.build_targets_override,
                tests_targets_override=self.data.tests_targets_override,
                pushgateway=self.pushgateway,
            )
        return self._copr_build_helper

    def run(self) -> TaskResults:
        if self.package_config.srpm_build_deps is not None:
            return self.copr_build_helper.run_copr_build_from_source_script()
        return self.copr_build_helper.run_copr_build()

    def pre_check(self) -> bool:
        if self.data.event_type in (
            PushGitHubEvent.__name__,
            PushGitlabEvent.__name__,
            PushPagureEvent.__name__,
        ):
            configured_branch = self.copr_build_helper.job_build_branch
            if self.data.git_ref != configured_branch:
                logger.info(
                    f"Skipping build on '{self.data.git_ref}'. "
                    f"Push configured only for '{configured_branch}'."
                )
                return False

        if not (self.copr_build_helper.job_build or self.copr_build_helper.job_tests):
            logger.info("No copr_build or tests job defined.")
            # we can't report it to end-user at this stage
            return False

        return True


class AbstractCoprBuildReportHandler(JobHandler):
    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        event: dict,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            event=event,
        )
        self.copr_event = AbstractCoprBuildEvent.from_event_dict(event)
        self._build = None
        self._db_trigger = None
        self._copr_build_helper: Optional[CoprBuildJobHelper] = None

    @property
    def copr_build_helper(self) -> CoprBuildJobHelper:
        # when reporting state of SRPM build built in Copr
        build_targets_override = (
            {
                build.target
                for build in CoprBuildTargetModel.get_all_by_build_id(
                    str(self.copr_event.build_id)
                )
            }
            if self.copr_event.chroot == COPR_SRPM_CHROOT
            else None
        )
        if not self._copr_build_helper:
            self._copr_build_helper = CoprBuildJobHelper(
                service_config=self.service_config,
                package_config=self.package_config,
                project=self.project,
                metadata=self.data,
                db_trigger=self.db_trigger,
                job_config=self.job_config,
                pushgateway=self.pushgateway,
                build_targets_override=build_targets_override,
            )
        return self._copr_build_helper

    @property
    def build(self):
        if not self._build:
            build_id = str(self.copr_event.build_id)
            if self.copr_event.chroot == COPR_SRPM_CHROOT:
                self._build = SRPMBuildModel.get_by_copr_build_id(build_id)
            else:
                self._build = CoprBuildTargetModel.get_by_build_id(
                    build_id, self.copr_event.chroot
                )
        return self._build

    @property
    def db_trigger(self) -> Optional[AbstractTriggerDbType]:
        if not self._db_trigger:
            self._db_trigger = self.build.get_trigger_object()
        return self._db_trigger


@configured_as(job_type=JobType.copr_build)
@configured_as(job_type=JobType.build)
@required_for(job_type=JobType.tests)
@reacts_to(event=CoprBuildStartEvent)
class CoprBuildStartHandler(AbstractCoprBuildReportHandler):
    topic = "org.fedoraproject.prod.copr.build.start"
    task_name = TaskName.copr_build_start

    def set_start_time(self):
        start_time = (
            datetime.utcfromtimestamp(self.copr_event.timestamp)
            if self.copr_event.timestamp
            else None
        )
        self.build.set_start_time(start_time)

    def set_logs_url(self):
        copr_build_logs = self.copr_event.get_copr_build_logs_url()
        self.build.set_build_logs_url(copr_build_logs)

    def pre_check(self) -> bool:
        if (
            self.copr_event.owner == self.copr_build_helper.job_owner
            and self.copr_event.project_name == self.copr_build_helper.job_project
        ):
            return True

        logger.debug(
            f"The Copr project {self.copr_event.owner}/{self.copr_event.project_name} "
            f"does not match the configuration "
            f"({self.copr_build_helper.job_owner}/{self.copr_build_helper.job_project} expected)."
        )
        return False

    def run(self):
        if not self.build:
            model = (
                "SRPMBuildDB"
                if self.copr_event.chroot == COPR_SRPM_CHROOT
                else "CoprBuildDB"
            )
            msg = f"Copr build {self.copr_event.build_id} not in {model}."
            logger.warning(msg)
            return TaskResults(success=False, details={"msg": msg})

        self.set_start_time()
        self.set_logs_url()

        if self.copr_event.chroot == COPR_SRPM_CHROOT:
            url = get_srpm_build_info_url(self.build.id)
            self.copr_build_helper.report_status_to_all(
                description="SRPM build is in progress...",
                state=BaseCommitStatus.running,
                url=url,
            )
            msg = "SRPM build in Copr has started..."
            return TaskResults(success=True, details={"msg": msg})

        self.pushgateway.copr_builds_started.inc()
        url = get_copr_build_info_url(self.build.id)
        self.build.set_status("pending")

        self.copr_build_helper.report_status_to_all_for_chroot(
            description="RPM build is in progress...",
            state=BaseCommitStatus.running,
            url=url,
            chroot=self.copr_event.chroot,
        )
        msg = f"Build on {self.copr_event.chroot} in copr has started..."
        return TaskResults(success=True, details={"msg": msg})


@configured_as(job_type=JobType.copr_build)
@configured_as(job_type=JobType.build)
@required_for(job_type=JobType.tests)
@reacts_to(event=CoprBuildEndEvent)
class CoprBuildEndHandler(AbstractCoprBuildReportHandler):
    topic = "org.fedoraproject.prod.copr.build.end"
    task_name = TaskName.copr_build_end

    def was_last_packit_comment_with_congratulation(self):
        """
        Check if the last comment by the packit app
        was about successful build to not duplicate it.

        :return: bool
        """
        comments = self.project.get_pr(self.copr_event.pr_id).get_comments(reverse=True)
        for comment in comments:
            if comment.author.startswith("packit-as-a-service"):
                return "Congratulations!" in comment.body
        # if there is no comment from p-s
        return False

    def set_srpm_url(self) -> None:
        # TODO how to do better
        srpm_build = (
            self.build
            if self.copr_event.chroot == COPR_SRPM_CHROOT
            else self.build.get_srpm_build()
        )

        if srpm_build.url is not None:
            # URL has been already set
            return

        srpm_url = self.copr_build_helper.get_build(
            self.copr_event.build_id
        ).source_package.get("url")

        if srpm_url is not None:
            srpm_build.set_url(srpm_url)

    def set_end_time(self):
        end_time = (
            datetime.utcfromtimestamp(self.copr_event.timestamp)
            if self.copr_event.timestamp
            else None
        )
        self.build.set_end_time(end_time)

    def run(self):
        if not self.build:
            # TODO: how could this happen?
            model = (
                "SRPMBuildDB"
                if self.copr_event.chroot == COPR_SRPM_CHROOT
                else "CoprBuildDB"
            )
            msg = f"Copr build {self.copr_event.build_id} not in {model}."
            logger.warning(msg)
            return TaskResults(success=False, details={"msg": msg})

        if self.build.status in [
            PG_BUILD_STATUS_FAILURE,
            PG_BUILD_STATUS_SUCCESS,
        ]:
            msg = (
                f"Copr build {self.copr_event.build_id} is already"
                f" processed (status={self.copr_event.build.status})."
            )
            logger.info(msg)
            return TaskResults(success=True, details={"msg": msg})

        self.set_end_time()
        self.set_srpm_url()

        if self.copr_event.chroot == COPR_SRPM_CHROOT:
            return self.handle_srpm_end()

        self.pushgateway.copr_builds_finished.inc()

        # if the build is needed only for test, it doesn't have the task_accepted_time
        if self.build.task_accepted_time:
            copr_build_time = measure_time(
                end=datetime.now(timezone.utc), begin=self.build.task_accepted_time
            )
            self.pushgateway.copr_build_finished_time.observe(copr_build_time)

        # https://pagure.io/copr/copr/blob/master/f/common/copr_common/enums.py#_42
        if self.copr_event.status != COPR_API_SUCC_STATE:
            failed_msg = "RPMs failed to be built."
            self.copr_build_helper.report_status_to_all_for_chroot(
                state=BaseCommitStatus.failure,
                description=failed_msg,
                url=get_copr_build_info_url(self.build.id),
                chroot=self.copr_event.chroot,
            )
            self.build.set_status(PG_BUILD_STATUS_FAILURE)
            return TaskResults(success=False, details={"msg": failed_msg})

        self.report_successful_build()
        self.build.set_status(PG_BUILD_STATUS_SUCCESS)

        built_packages = self.copr_build_helper.get_built_packages(
            int(self.build.build_id), self.build.target
        )
        self.build.set_built_packages(built_packages)
        self.handle_testing_farm()

        return TaskResults(success=True, details={})

    def report_successful_build(self):
        if (
            self.copr_build_helper.job_build
            and self.copr_build_helper.job_build.trigger
            == JobConfigTriggerType.pull_request
            and self.copr_event.pr_id
            and isinstance(self.project, (GithubProject, GitlabProject))
            and not self.was_last_packit_comment_with_congratulation()
            and self.job_config.notifications.pull_request.successful_build
        ):
            msg = (
                f"Congratulations! One of the builds has completed. :champagne:\n\n"
                "You can install the built RPMs by following these steps:\n\n"
                "* `sudo yum install -y dnf-plugins-core` on RHEL 8\n"
                "* `sudo dnf install -y dnf-plugins-core` on Fedora\n"
                f"* `dnf copr enable {self.copr_event.owner}/{self.copr_event.project_name}`\n"
                "* And now you can install the packages.\n"
                "\nPlease note that the RPMs should be used only in a testing environment."
            )
            self.project.get_pr(self.copr_event.pr_id).comment(msg)

        url = get_copr_build_info_url(self.build.id)

        self.copr_build_helper.report_status_to_build_for_chroot(
            state=BaseCommitStatus.success,
            description="RPMs were built successfully.",
            url=url,
            chroot=self.copr_event.chroot,
        )
        self.copr_build_helper.report_status_to_test_for_chroot(
            state=BaseCommitStatus.pending,
            description="RPMs were built successfully.",
            url=url,
            chroot=self.copr_event.chroot,
        )

    def handle_srpm_end(self):
        url = get_srpm_build_info_url(self.build.id)

        if self.copr_event.status != COPR_API_SUCC_STATE:
            failed_msg = "SRPM build failed, check the logs for details."
            self.copr_build_helper.report_status_to_all(
                state=BaseCommitStatus.failure,
                description=failed_msg,
                url=url,
            )
            self.build.set_status(PG_BUILD_STATUS_FAILURE)
            self.copr_build_helper.monitor_not_submitted_copr_builds(
                len(self.copr_build_helper.build_targets), "srpm_failure"
            )
            return TaskResults(success=False, details={"msg": failed_msg})

        for build in CoprBuildTargetModel.get_all_by_build_id(
            str(self.copr_event.build_id)
        ):
            # from waiting_for_srpm to pending
            build.set_status("pending")

        self.build.set_status(PG_BUILD_STATUS_SUCCESS)
        self.copr_build_helper.report_status_to_all(
            state=BaseCommitStatus.running,
            description="SRPM build succeeded. Waiting for RPM build to start...",
            url=url,
        )
        msg = "SRPM build in Copr has finished."
        logger.debug(msg)
        return TaskResults(success=True, details={"msg": msg})

    def handle_testing_farm(self):
        if (
            self.copr_build_helper.job_tests
            and self.copr_event.chroot in self.copr_build_helper.build_targets_for_tests
        ):
            event_dict = self.data.get_dict()
            event_dict["tests_targets_override"] = list(
                self.copr_build_helper.build_target2test_targets(self.copr_event.chroot)
            )
            signature(
                TaskName.testing_farm.value,
                kwargs={
                    "package_config": dump_package_config(self.package_config),
                    "job_config": dump_job_config(self.copr_build_helper.job_tests),
                    "event": event_dict,
                    "build_id": self.build.id,
                },
            ).apply_async()
        else:
            logger.debug("Testing farm not in the job config.")
