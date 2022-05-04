# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

"""
This file defines classes for job handlers specific for Github hooks
TODO: The build and test handlers are independent and should be moved away.
"""
import logging

from packit.config import (
    JobConfig,
)
from packit.config.package_config import PackageConfig

from packit_service.models import (
    GithubInstallationModel,
)
from packit_service.worker.events import (
    InstallationEvent,
)
from packit_service.worker.allowlist import Allowlist
from packit_service.worker.handlers.abstract import (
    JobHandler,
    TaskName,
    reacts_to,
)
from packit_service.worker.result import TaskResults

logger = logging.getLogger(__name__)


@reacts_to(event=InstallationEvent)
class GithubAppInstallationHandler(JobHandler):
    task_name = TaskName.installation

    # https://developer.github.com/v3/activity/events/types/#events-api-payload-28

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

        self.installation_event = InstallationEvent.from_event_dict(event)
        self.account_type = self.installation_event.account_type
        self.account_login = self.installation_event.account_login
        self.sender_login = self.installation_event.sender_login
        self._project = self.service_config.get_project(
            url="https://github.com/packit/notifications"
        )

    def run(self) -> TaskResults:
        """
        Discover information about organization/user which wants to install packit on his repository
        Try to allowlist automatically if mapping from github username to FAS account can prove that
        user is a packager.
        :return: TaskResults
        """
        GithubInstallationModel.create(event=self.installation_event)
        # try to add user to allowlist
        allowlist = Allowlist(self.service_config)
        if not allowlist.add_namespace(
            f"github.com/{self.account_login}", self.sender_login
        ):
            # Create an issue in our repository, so we are notified when someone install the app
            self.project.create_issue(
                title=f"{self.account_type} {self.account_login} needs to be approved.",
                body=(
                    f"Hi @{self.sender_login}, we need to approve you in "
                    "order to start using Packit-as-a-Service. "
                    "We are now onboarding Fedora contributors who have a valid "
                    "[Fedora Account System](https://fedoraproject.org/wiki/Account_System) "
                    "account. If you have such an account, please, provide it in a comment and "
                    "we'd be glad to approve you for using the service.\n\n"
                    "For more info, please check out the documentation: "
                    "https://packit.dev/docs/packit-service"
                ),
            )
            msg = f"{self.account_type} {self.account_login} needs to be approved manually!"
        else:
            msg = (
                f"{self.account_type} {self.account_login} is already on our allowlist."
            )

        logger.info(msg)
        return TaskResults(success=True, details={"msg": msg})
