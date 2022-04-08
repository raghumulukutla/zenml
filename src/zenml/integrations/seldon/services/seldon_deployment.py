#  Copyright (c) ZenML GmbH 2022. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from uuid import UUID

import numpy as np
import requests  # type: ignore [import]
from pydantic import Field, ValidationError

from zenml import __version__
from zenml.integrations.seldon.seldon_client import (
    SeldonClient,
    SeldonDeployment,
    SeldonDeploymentNotFoundError,
)
from zenml.logger import get_logger
from zenml.repository import Repository
from zenml.secrets_managers.base_secrets_manager import BaseSecretsManager
from zenml.services.service import BaseService, ServiceConfig
from zenml.services.service_status import ServiceState, ServiceStatus
from zenml.services.service_type import ServiceType

if TYPE_CHECKING:
    from numpy.typing import NDArray

logger = get_logger(__name__)


class SeldonDeploymentConfig(ServiceConfig):
    """Seldon Core deployment service configuration.

    Attributes:
        model_uri: URI of the model (or models) to serve.
        model_name: the name of the model. Multiple versions of the same model
            should use the same model name.
        implementation: the Seldon Core implementation used to serve the model.
        replicas: number of replicas to use for the prediction service.
        secrets: a list of ZenML secrets containing additional configuration
            parameters for the Seldon Core deployment (e.g. credentials to
            access the Artifact Store where the models are stored). If supplied,
            the information fetched from these secrets is passed to the Seldon
            Core deployment server as a list of environment variables.
        model_metadata: optional model metadata information (see
            https://docs.seldon.io/projects/seldon-core/en/latest/reference/apis/metadata.html).
        extra_args: additional arguments to pass to the Seldon Core deployment
            resource configuration.
    """

    model_uri: str = ""
    model_name: str = "default"
    # TODO [ENG-775]: have an enum of all supported Seldon Core implementations
    implementation: str
    replicas: int = 1
    secrets: List[str] = Field(default_factory=list)
    model_metadata: Dict[str, Any] = Field(default_factory=dict)
    extra_args: Dict[str, Any] = Field(default_factory=dict)

    def get_seldon_deployment_labels(self) -> Dict[str, str]:
        """Generate the labels for the Seldon Core deployment from the
        service configuration.

        These labels are attached to the Seldon Core deployment resource
        and may be used as label selectors in lookup operations.

        Returns:
            The labels for the Seldon Core deployment.
        """
        labels = {}
        if self.pipeline_name:
            labels["zenml.pipeline_name"] = self.pipeline_name
        if self.pipeline_run_id:
            labels["zenml.pipeline_run_id"] = self.pipeline_run_id
        if self.pipeline_step_name:
            labels["zenml.pipeline_step_name"] = self.pipeline_step_name
        if self.model_name:
            labels["zenml.model_name"] = self.model_name
        if self.model_uri:
            labels["zenml.model_uri"] = self.model_uri
        if self.implementation:
            labels["zenml.model_type"] = self.implementation
        SeldonClient.sanitize_labels(labels)
        return labels

    def get_seldon_deployment_annotations(self) -> Dict[str, str]:
        """Generate the annotations for the Seldon Core deployment from the
        service configuration.

        The annotations are used to store additional information about the
        Seldon Core service that is associated with the deployment that is
        not available in the labels. One annotation particularly important
        is the serialized Service configuration itself, which is used to
        recreate the service configuration from a remote Seldon deployment.

        Returns:
            The annotations for the Seldon Core deployment.
        """
        annotations = {
            "zenml.service_config": self.json(),
            "zenml.version": __version__,
        }
        return annotations

    @classmethod
    def create_from_deployment(
        cls, deployment: SeldonDeployment
    ) -> "SeldonDeploymentConfig":
        """Recreate a Seldon Core service configuration from a Seldon Core
        deployment resource.

        Args:
            deployment: the Seldon Core deployment resource.

        Returns:
            The Seldon Core service configuration corresponding to the given
            Seldon Core deployment resource.

        Raises:
            ValueError: if the given deployment resource does not contain
                the expected annotations or it contains an invalid or
                incompatible Seldon Core service configuration.
        """
        config_data = deployment.metadata.annotations.get(
            "zenml.service_config"
        )
        if not config_data:
            raise ValueError(
                f"The given deployment resource does not contain a "
                f"'zenml.service_config' annotation: {deployment}"
            )
        try:
            service_config = cls.parse_raw(config_data)
        except ValidationError as e:
            raise ValueError(
                f"The loaded Seldon Core deployment resource contains an "
                f"invalid or incompatible Seldon Core service configuration: "
                f"{config_data}"
            ) from e
        return service_config


class SeldonDeploymentServiceStatus(ServiceStatus):
    """Seldon Core deployment service status."""


class SeldonDeploymentService(BaseService):
    """A service that represents a Seldon Core deployment server.


    Attributes:
        config: service configuration.
        status: service status.
    """

    SERVICE_TYPE = ServiceType(
        name="seldon-deployment",
        type="model-serving",
        flavor="seldon",
        description="Seldon Core prediction service",
    )

    config: SeldonDeploymentConfig = Field(
        default_factory=SeldonDeploymentConfig
    )
    status: SeldonDeploymentServiceStatus = Field(
        default_factory=SeldonDeploymentServiceStatus
    )

    def _get_client(self) -> SeldonClient:
        """Get the Seldon Core client from the active Seldon Core model deployer.

        Returns:
            The Seldon Core client.
        """
        from zenml.integrations.seldon.model_deployers.seldon_model_deployer import (
            SeldonModelDeployer,
        )

        model_deployer = SeldonModelDeployer.get_active_model_deployer()
        return model_deployer.seldon_client

    def check_status(self) -> Tuple[ServiceState, str]:
        """Check the the current operational state of the Seldon Core
        deployment.

        Returns:
            The operational state of the Seldon Core deployment and a message
            providing additional information about that state (e.g. a
            description of the error, if one is encountered).
        """
        client = self._get_client()
        name = self.seldon_deployment_name
        try:
            deployment = client.get_deployment(name=name)
        except SeldonDeploymentNotFoundError:
            return (ServiceState.INACTIVE, "")

        if deployment.is_available():
            return (
                ServiceState.ACTIVE,
                f"Seldon Core deployment '{name}' is available",
            )

        if deployment.is_failed():
            return (
                ServiceState.ERROR,
                f"Seldon Core deployment '{name}' failed: "
                f"{deployment.get_error()}",
            )

        pending_message = deployment.get_pending_message() or ""
        return (
            ServiceState.PENDING_STARTUP,
            "Seldon Core deployment is being created: " + pending_message,
        )

    @property
    def seldon_deployment_name(self) -> str:
        """Get the name of the Seldon Core deployment that uniquely
        corresponds to this service instance

        Returns:
            The name of the Seldon Core deployment.
        """
        return f"zenml-{str(self.uuid)}"

    def _get_seldon_deployment_labels(self) -> Dict[str, str]:
        """Generate the labels for the Seldon Core deployment from the
        service configuration.

        Returns:
            The labels for the Seldon Core deployment.
        """
        labels = self.config.get_seldon_deployment_labels()
        labels["zenml.service_uuid"] = str(self.uuid)
        SeldonClient.sanitize_labels(labels)
        return labels

    @classmethod
    def create_from_deployment(
        cls, deployment: SeldonDeployment
    ) -> "SeldonDeploymentService":
        """Recreate a Seldon Core service from a Seldon Core
        deployment resource and update their operational status.

        Args:
            deployment: the Seldon Core deployment resource.

        Returns:
            The Seldon Core service corresponding to the given
            Seldon Core deployment resource.
        """
        config = SeldonDeploymentConfig.create_from_deployment(deployment)
        uuid = deployment.metadata.labels.get("zenml.service_uuid")
        if not uuid:
            raise ValueError(
                f"The given deployment resource does not contain a valid "
                f"'zenml.service_uuid' label: {deployment}"
            )
        service = cls(uuid=UUID(uuid), config=config)
        service.update_status()
        return service

    def provision(self) -> None:
        """Provision or update the remote Seldon Core deployment instance to
        match the current configuration.
        """
        client = self._get_client()

        name = self.seldon_deployment_name

        # if a list of ZenML secrets were supplied in the service config,
        # create a Kubernetes secret as a means to pass this information
        # to the Seldon Core deployment
        if self.config.secrets:
            secret_manager = Repository(  # type: ignore [call-arg]
                skip_repository_check=True
            ).active_stack.secrets_manager

            if not secret_manager or not isinstance(
                secret_manager, BaseSecretsManager
            ):
                raise RuntimeError(
                    f"The active stack doesn't have a secret manager component. "
                    f"The ZenML secrets specified in the Seldon Core service "
                    f"configuration cannot be fetched: {self.config.secrets}."
                )

            zenml_secrets = []
            for secret_name in self.config.secrets:
                try:
                    zenml_secrets.append(secret_manager.get_secret(secret_name))
                except KeyError:
                    raise RuntimeError(
                        f"The ZenML secret '{secret_name}' specified in the "
                        f"Seldon Core service configuration does not exist: "
                        f"{self.config.secrets}"
                    )

            client.create_or_update_secret(name, zenml_secrets)

        deployment = SeldonDeployment.build(
            name=name,
            model_uri=self.config.model_uri,
            model_name=self.config.model_name,
            implementation=self.config.implementation,
            secret_name=name if self.config.secrets else None,
            labels=self._get_seldon_deployment_labels(),
            annotations=self.config.get_seldon_deployment_annotations(),
        )
        deployment.spec.replicas = self.config.replicas
        deployment.spec.predictors[0].replicas = self.config.replicas

        # check if the Seldon deployment already exists
        try:
            client.get_deployment(name=name)
            # update the existing deployment
            client.update_deployment(deployment)
        except SeldonDeploymentNotFoundError:
            # create the deployment
            client.create_deployment(deployment=deployment)

    def deprovision(self, force: bool = False) -> None:
        """Deprovision the remote Seldon Core deployment instance.

        Args:
            force: if True, the remote deployment instance will be
                forcefully deprovisioned.
        """
        client = self._get_client()
        name = self.seldon_deployment_name
        try:
            client.delete_deployment(name=name, force=force)
        except SeldonDeploymentNotFoundError:
            pass

        # if a list of ZenML secrets were supplied in the service config,
        # also delete the Kubernetes secret that was used to pass this
        # information to the Seldon Core deployment
        if self.config.secrets:
            client.delete_secret(name)

    @property
    def prediction_url(self) -> Optional[str]:
        """The prediction URI exposed by the prediction service.

        Returns:
            The prediction URI exposed by the prediction service, or None if
            the service is not yet ready.
        """
        from zenml.integrations.seldon.model_deployers.seldon_model_deployer import (
            SeldonModelDeployer,
        )

        if not self.is_running:
            return None
        namespace = self._get_client().namespace
        model_deployer = SeldonModelDeployer.get_active_model_deployer()
        return os.path.join(
            model_deployer.base_url,
            "seldon",
            namespace,
            self.seldon_deployment_name,
            "api/v0.1/predictions",
        )

    def predict(self, request: "NDArray[Any]") -> "NDArray[Any]":
        """Make a prediction using the service.

        Args:
            request: a numpy array representing the request

        Returns:
            A numpy array representing the prediction returned by the service.
        """
        if not self.is_running:
            raise Exception(
                "Seldon prediction service is not running. "
                "Please start the service before making predictions."
            )

        response = requests.post(
            self.prediction_url,
            json={"data": {"ndarray": request.tolist()}},
        )
        response.raise_for_status()
        return np.array(response.json()["data"]["ndarray"])