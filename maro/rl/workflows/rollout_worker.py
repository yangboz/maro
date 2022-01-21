# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from maro.rl.rollout import RolloutWorker
from maro.rl.utils.common import from_env, from_env_as_int, get_module
from maro.rl.workflows.utils import ScenarioAttr, _get_scenario_path

if __name__ == "__main__":
    scenario = get_module(_get_scenario_path())
    scenario_attr = ScenarioAttr(scenario)
    policy_creator = scenario_attr.policy_creator

    worker = RolloutWorker(
        idx=from_env_as_int("ID"),
        env_sampler_creator=lambda: scenario_attr.get_env_sampler(policy_creator),
        router_host=str(from_env("ROLLOUT_PROXY_HOST")),
        router_port=from_env_as_int("ROLLOUT_PROXY_BACKEND_PORT")
    )
    worker.start()
