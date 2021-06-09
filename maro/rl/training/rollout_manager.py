# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import time
from abc import ABC, abstractmethod
from collections import defaultdict
from os import getcwd
from random import choices
from typing import Dict, List

from maro.communication import Proxy, SessionType
from maro.rl.env_wrapper import AbsEnvWrapper
from maro.rl.experience import ExperienceSet
from maro.rl.exploration import AbsExploration
from maro.rl.policy import AbsPolicy
from maro.utils import Logger

from .message_enums import MsgKey, MsgTag


class AbsRolloutManager(ABC):
    """Controller for simulation data collection."""
    def __init__(self):
        super().__init__()
        self.episode_complete = False

    @abstractmethod
    def collect(self, ep: int, segment: int, policy_state_dict: dict):
        """Collect simulation data, i.e., experiences for training.

        Args:
            ep (int): Current episode index.
            segment (int): Current segment index.
            policy_state_dict (dict): Policy states to use for simulation.

        Returns:
            Experiences for policy training.
        """
        raise NotImplementedError

    @abstractmethod
    def evaluate(self, ep: int, policy_state_dict: dict):
        """Evaluate the performance of ``policy_state_dict``.

        Args:
            ep (int): Current training episode index.
            policy_state_dict (dict): Policy states to use for simulation.

        Returns:
            Environment summary.
        """
        raise NotImplementedError

    def reset(self):
        self.episode_complete = False


class LocalRolloutManager(AbsRolloutManager):
    """Controller for a single local roll-out actor.

    Args:
        env (AbsEnvWrapper): An ``AbsEnvWrapper`` instance to interact with a set of agents and collect experiences
            for policy training / update.
        policies (List[AbsPolicy]): A set of named policies for inference.
        agent2policy (Dict[str, str]): Mapping from agent ID's to policy ID's. This is used to direct an agent's
            queries to the correct policy.
        exploration_dict (Dict[str, AbsExploration]): A set of named exploration schemes. Defaults to None.
        agent2exploration (Dict[str, str]): Mapping from agent ID's to exploration scheme ID's. This is used to direct
            an agent's query to the correct exploration scheme. Defaults to None.
        num_steps (int): Number of environment steps to roll out in each call to ``collect``. Defaults to -1, in which
            case the roll-out will be executed until the end of the environment.
        eval_env (AbsEnvWrapper): An ``AbsEnvWrapper`` instance for policy evaluation. If None, ``env`` will be used
            as the evaluation environment. Defaults to None.
        log_env_summary (bool): If True, the ``summary`` property of the environment wrapper will be logged at the end of
            each episode. Defaults to True.
        log_dir (str): Directory to store logs in. A ``Logger`` with tag "LOCAL_ROLLOUT_MANAGER" will be created at
            init time and this directory will be used to save the log files generated by it. Defaults to the current
            working directory.
    """

    def __init__(
        self,
        env: AbsEnvWrapper,
        policies: List[AbsPolicy],
        agent2policy: Dict[str, str],
        exploration_dict: Dict[str, AbsExploration] = None,
        agent2exploration: Dict[str, str] = None,
        num_steps: int = -1,
        eval_env: AbsEnvWrapper = None,
        log_env_summary: bool = True,
        log_dir: str = getcwd(),
    ):
        if num_steps == 0 or num_steps < -1:
            raise ValueError("num_steps must be a positive integer or -1")

        self._logger = Logger("LOCAL_ROLLOUT_MANAGER", dump_folder=log_dir)

        self.env = env
        self.eval_env = eval_env if eval_env else self.env

        # mappings between agents and policies
        self.policy_dict = {policy.name: policy for policy in policies}
        self._agent2policy = agent2policy
        self._policy = {
            agent_id: self.policy_dict[policy_name] for agent_id, policy_name in self._agent2policy.items()
        }
        self._agent_groups_by_policy = defaultdict(list)
        for agent_id, policy_name in agent2policy.items():
            self._agent_groups_by_policy[policy_name].append(agent_id)

        # mappings between exploration schemes and agents
        self.exploration_dict = exploration_dict
        self._agent_groups_by_exploration = defaultdict(list)
        if exploration_dict:
            self._agent2exploration = agent2exploration
            self._exploration = {
                agent_id: self.exploration_dict[exploration_id]
                for agent_id, exploration_id in self._agent2exploration.items()
            }
            for agent_id, exploration_id in self._agent2exploration.items():
                self._agent_groups_by_exploration[exploration_id].append(agent_id)

        self._num_steps = num_steps if num_steps > 0 else float("inf")
        self._log_env_summary = log_env_summary

    def collect(self, ep: int, segment: int, policy_state_dict: dict):
        """Collect simulation data for training."""
        t0 = time.time()
        learning_time = 0
        num_experiences_collected = 0

        if self.exploration_dict:
            exploration_params = {
                tuple(agent_ids): self.exploration_dict[exploration_id].parameters
                for exploration_id, agent_ids in self._agent_groups_by_exploration.items()
            }
            self._logger.debug(f"Exploration parameters: {exploration_params}")

        if self.env.state is None:
            self._logger.info(f"Collecting data from episode {ep}, segment {segment}")
            if self.exploration_dict:
                exploration_params = {
                    tuple(agent_ids): self.exploration_dict[exploration_id].parameters
                    for exploration_id, agent_ids in self._agent_groups_by_exploration.items()
                }
                self._logger.debug(f"Exploration parameters: {exploration_params}")

            self.env.reset()
            self.env.start()  # get initial state

        # load policies
        self._load_policy_states(policy_state_dict)

        start_step_index = self.env.step_index + 1
        steps_to_go = self._num_steps
        while self.env.state and steps_to_go > 0:
            if self.exploration_dict:
                action = {
                    id_:
                        self._exploration[id_](self._policy[id_].choose_action(st))
                        if id_ in self._exploration else self._policy[id_].choose_action(st)
                    for id_, st in self.env.state.items()
                }
            else:
                action = {id_: self._policy[id_].choose_action(st) for id_, st in self.env.state.items()}

            self.env.step(action)
            steps_to_go -= 1

        self._logger.info(
            f"Roll-out finished for ep {ep}, segment {segment}"
            f"(steps {start_step_index} - {self.env.step_index})"
        )

        # update the exploration parameters if an episode is finished
        if not self.env.state:
            self.episode_complete = True
            if self.exploration_dict:
                for exploration in self.exploration_dict.values():
                    exploration.step()

            # performance details
            if self._log_env_summary:
                self._logger.info(f"ep {ep}: {self.env.summary}")

            self._logger.debug(
                f"ep {ep} summary - "
                f"running time: {time.time() - t0} "
                f"env steps: {self.env.step_index} "
                f"learning time: {learning_time} "
                f"experiences collected: {num_experiences_collected}"
            )

        return self.env.get_experiences()

    def evaluate(self, ep: int, policy_state_dict: dict):
        """Evaluate the performance of ``policy_state_dict``.

        Args:
            ep (int): Current training episode index.
            policy_state_dict (dict): Policy states to use for simulation.

        Returns:
            Environment summary.
        """
        self._logger.info("Evaluating...")
        self._load_policy_states(policy_state_dict)
        self.eval_env.reset()
        self.eval_env.start()  # get initial state
        while self.eval_env.state:
            action = {id_: self._policy[id_].choose_action(st) for id_, st in self.eval_env.state.items()}
            self.eval_env.step(action)

        if self._log_env_summary:
            self._logger.info(f"Evaluation result: {self.eval_env.summary}")

        return self.eval_env.summary

    def _load_policy_states(self, policy_state_dict: dict):
        for policy_name, policy_state in policy_state_dict.items():
            self.policy_dict[policy_name].set_state(policy_state)

        if policy_state_dict:
            self._logger.info(f"updated policies {list(policy_state_dict.keys())}")


class ParallelRolloutManager(AbsRolloutManager):
    """Controller for a set of remote roll-out actors.

    Args:
        num_actors (int): Number of remote roll-out actors.
        group (str): Identifier of the group to which the actor belongs. It must be the same group name
            assigned to the learner (and decision clients, if any).
        exploration_dict (Dict[str, AbsExploration]): A set of named exploration schemes. The exploration parameters
            from these instances will be broadcast to all actors. Defaults to None.
        num_steps (int): Number of environment steps to roll out in each call to ``collect``. Defaults to -1, in which
            case the roll-out will be executed until the end of the environment.
        max_receive_attempts (int): Maximum number of attempts to receive actor results in ``collect``. Defaults to
            None, in which case the number is set to ``num_actors``.
        receive_timeout (int): Maximum wait time (in milliseconds) for each attempt to receive from the actors. This
            This multiplied by ``max_receive_attempts`` give the upperbound for the amount of time to receive the
            desired amount of data from actors. Defaults to None, in which case each receive attempt is blocking.
        max_staleness (int): Maximum allowable staleness measured in the number of calls to ``collect``. Experiences
            collected from calls to ``collect`` within ``max_staleness`` calls ago will be returned to the learner.
            Defaults to 0, in which case only experiences from the latest call to ``collect`` will be returned.
        num_eval_actors (int): Number of actors required for evaluation. Defaults to 1.
        log_env_summary (bool): If True, the ``summary`` property of the environment wrapper will be logged at the end of
            each episode. Defaults to True.
        log_dir (str): Directory to store logs in. A ``Logger`` with tag "LOCAL_ROLLOUT_MANAGER" will be created at
            init time and this directory will be used to save the log files generated by it. Defaults to the current
            working directory.
        proxy_kwargs: Keyword parameters for the internal ``Proxy`` instance. See ``Proxy`` class
            for details.
    """
    def __init__(
        self,
        num_actors: int,
        group: str,
        exploration_dict: Dict[str, AbsExploration] = None,
        num_steps: int = -1,
        max_receive_attempts: int = None,
        receive_timeout: int = None,
        max_staleness: int = 0,
        num_eval_actors: int = 1,
        log_env_summary: bool = True,
        log_dir: str = getcwd(),
        **proxy_kwargs
    ):
        super().__init__()
        if num_eval_actors > num_actors:
            raise ValueError("num_eval_actors cannot exceed the number of available actors")

        self._logger = Logger("PARALLEL_ROLLOUT_MANAGER", dump_folder=log_dir)
        self.num_actors = num_actors
        peers = {"actor": num_actors}
        self._proxy = Proxy(group, "rollout_manager", peers, **proxy_kwargs)
        self._actors = self._proxy.peers["actor"]  # remote actor ID's

        self.exploration_dict = exploration_dict
        self._num_steps = num_steps

        if max_receive_attempts is None:
            max_receive_attempts = self.num_actors
            self._logger.info(f"Maximum receive attempts is set to {max_receive_attempts}")

        self.max_receive_attempts = max_receive_attempts
        self.receive_timeout = receive_timeout

        self._max_staleness = max_staleness
        self.total_experiences_collected = 0
        self.total_env_steps = 0
        self._log_env_summary = log_env_summary

        self._num_eval_actors = num_eval_actors

        self._exploration_update = True

    def collect(self, episode_index: int, segment_index: int, policy_state_dict: dict):
        """Collect simulation data, i.e., experiences for training."""
        if self._log_env_summary:
            self._logger.info(f"EPISODE-{episode_index}, SEGMENT-{segment_index}: ")

        msg_body = {
            MsgKey.EPISODE_INDEX: episode_index,
            MsgKey.SEGMENT_INDEX: segment_index,
            MsgKey.NUM_STEPS: self._num_steps,
            MsgKey.POLICY: policy_state_dict
        }

        if self._exploration_update:
            msg_body[MsgKey.EXPLORATION] = {
                name: exploration.parameters for name, exploration in self.exploration_dict.items()
            }
            self._exploration_update = False

        self._proxy.ibroadcast("actor", MsgTag.COLLECT, SessionType.TASK, body=msg_body)
        self._logger.info(f"Sent collect requests to {self._actors} for ep-{episode_index}, segment-{segment_index}")

        # Receive roll-out results from remote actors
        combined_exp_by_policy = defaultdict(ExperienceSet)
        num_finishes = 0
        for _ in range(self.max_receive_attempts):
            msg = self._proxy.receive_once(timeout=self.receive_timeout)
            if msg.tag != MsgTag.COLLECT_DONE or msg.body[MsgKey.EPISODE_INDEX] != episode_index:
                self._logger.info(
                    f"Ignore a message of type {msg.tag} with episode index {msg.body[MsgKey.EPISODE_INDEX]} "
                    f"(expected message type {MsgTag.COLLECT} and episode index {episode_index})"
                )
                continue

            if segment_index - msg.body[MsgKey.SEGMENT_INDEX] <= self._max_staleness:
                exp_by_policy = msg.body[MsgKey.EXPERIENCES]
                self.total_experiences_collected += sum(exp.size for exp in exp_by_policy.values())
                self.total_env_steps += msg.body[MsgKey.NUM_STEPS]

                for policy_name, exp in exp_by_policy.items():
                    combined_exp_by_policy[policy_name].extend(exp)

                if msg.body[MsgKey.SEGMENT_INDEX] == segment_index:
                    self.episode_complete = msg.body[MsgKey.EPISODE_END]
                    if self.episode_complete:
                        # log roll-out summary
                        if self._log_env_summary:
                            self._logger.info(f"env summary: {msg.body[MsgKey.ENV_SUMMARY]}")
                    num_finishes += 1
                    if num_finishes == self.num_actors:
                        break
        
        if self.episode_complete:
            if self.exploration_dict:
                for exploration in self.exploration_dict.values():
                    exploration.step()
                self._exploration_update = True

        return combined_exp_by_policy

    def evaluate(self, ep: int, policy_state_dict: dict):
        """Evaluate the performance of ``policy_state_dict``.

        Args:
            ep (int): Current training episode index.
            policy_state_dict (dict): Policy states to use for simulation.

        Returns:
            Environment summary.
        """
        msg_body = {MsgKey.EPISODE_INDEX: ep, MsgKey.POLICY: policy_state_dict}

        actors = choices(self._actors, k=self._num_eval_actors)
        env_summary_dict = {}
        self._proxy.iscatter(MsgTag.EVAL, SessionType.TASK, [(actor_id, msg_body) for actor_id in actors])
        self._logger.info(f"Sent evaluation requests to {actors}")

        # Receive roll-out results from remote actors
        num_finishes = 0
        for msg in self._proxy.receive():
            if msg.tag != MsgTag.EVAL_DONE or msg.body[MsgKey.EPISODE_INDEX] != ep:
                self._logger.info(
                    f"Ignore a message of type {msg.tag} with episode index {msg.body[MsgKey.EPISODE_INDEX]} "
                    f"(expected message type {MsgTag.EVAL_DONE} and episode index {ep})"
                )
                continue

            env_summary_dict[msg.source] = msg.body[MsgKey.ENV_SUMMARY]

            if msg.body[MsgKey.EPISODE_INDEX] == ep:
                num_finishes += 1
                if num_finishes == self._num_eval_actors:
                    break

        return env_summary_dict

    def exit(self):
        """Tell the remote actors to exit."""
        self._proxy.ibroadcast("actor", MsgTag.EXIT, SessionType.NOTIFICATION)
        self._proxy.close()
        self._logger.info("Exiting...")
