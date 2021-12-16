# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from typing import List, Optional

from maro.simulator import Env
from maro.simulator.scenarios.oncall_routing.common import Action, OncallRoutingPayload
from maro.utils import set_seeds

set_seeds(0)


def _is_equal_segment(action1: Action, action2: Action) -> bool:
    return (action1.route_name, action1.insert_index) == (action2.route_name, action2.insert_index)


def _get_actions(running_env: Env, event: OncallRoutingPayload) -> List[Action]:
    tick = running_env.tick
    oncall_orders = event.oncall_orders
    route_meta_info_dict = event.route_meta_info_dict
    route_plan_dict = event.route_plan_dict
    carriers_in_stop: List[bool] = (running_env.snapshot_list["carriers"][tick::"in_stop"] == 1).tolist()
    est_duration_predictor = event.estimated_duration_predictor

    actions = []
    for oncall_order in oncall_orders:
        min_distance = float("inf")
        chosen_route_name: Optional[str] = None
        insert_idx = -1

        for route_name, meta in route_meta_info_dict.items():
            carrier_idx = meta["carrier_idx"]
            planned_orders = route_plan_dict[route_name]

            for i, planned_order in enumerate(planned_orders):
                if i == 0 and not carriers_in_stop[carrier_idx]:
                    continue
                distance = est_duration_predictor.predict(tick, oncall_order.coord, planned_order.coord)
                if distance < min_distance:
                    min_distance, chosen_route_name, insert_idx = distance, route_name, i

        if chosen_route_name is not None:
            actions.append(Action(order_id=oncall_order.id, route_name=chosen_route_name, insert_index=insert_idx))

    # Add segment index if multiple orders are share
    actions.sort(key=lambda action: (action.route_name, action.insert_index))
    segment_index = 0
    for idx in range(len(actions) - 1):
        if _is_equal_segment(actions[idx], actions[idx + 1]):
            segment_index += 1
            actions[idx + 1].in_segment_order = segment_index
        else:
            segment_index = 0

    return actions


# Greedy: assign each on-call order to the closest stop on existing route.
if __name__ == "__main__":
    env = Env(
        scenario="oncall_routing", topology="example", start_tick=0, durations=1440,
    )

    env.reset(keep_seed=True)
    metrics, decision_event, is_done = env.step(None)
    while not is_done:
        assert isinstance(decision_event, OncallRoutingPayload)
        print(f"Processing {len(decision_event.oncall_orders)} oncall orders at tick {env.tick}.")
        metrics, decision_event, is_done = env.step(_get_actions(env, decision_event))

    print(metrics)
