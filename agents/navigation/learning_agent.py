#!/usr/bin/env python

import carla
from agents.navigation.agent import Agent, AgentState
from agents.navigation.local_planner import LocalPlanner
from agents.navigation.global_route_planner import GlobalRoutePlanner
from agents.navigation.global_route_planner_dao import GlobalRoutePlannerDAO


class LearningAgent(Agent):
    """
    BasicAgent implements a basic agent that navigates scenes to reach a given
    target destination. This agent respects traffic lights and other vehicles.
    """

    def __init__(self, vehicle, sensors, target_speed=30.0):
        """

        :param vehicle: actor to apply to local planner logic onto
        """
        super(LearningAgent, self).__init__(vehicle)

        self._sensors = sensors
        self._proximity_threshold = 10.0  # meters
        self._state = AgentState.NAVIGATING
        args_lateral_dict = {'K_P': 1.0, 'K_I': 0.4, 'K_D': 0.01}
        args_longitudinal_dict = {'K_P': 1.0, 'K_I': 0.4, 'K_D': 0.05}
        self._local_planner = LocalPlanner(self._vehicle,
                                           opt_dict={'target_speed': target_speed,
                                                     'lateral_control_dict': args_lateral_dict,
                                                     'longitudinal_control_dict': args_longitudinal_dict})
        self._hop_resolution = 2.0
        self._path_seperation_hop = 2
        self._path_seperation_threshold = 0.5
        self._target_speed = target_speed

        self._grp = None

        self._safe_distance = 15.0

    # Set global destination and get global waypoints
    def set_destination(self, location):
        """
        This method creates a list of waypoints from agent's position to destination location
        based on the route returned by the global router
        """
        start_waypoint = self._map.get_waypoint(self._vehicle.get_location())
        end_waypoint = self._map.get_waypoint(carla.Location(location[0], location[1], location[2]))

        route_trace = self._trace_route(start_waypoint, end_waypoint)
        assert route_trace

        self._local_planner.set_global_plan(route_trace)

    # Get global waypoints
    def _trace_route(self, start_waypoint, end_waypoint):
        """
        This method sets up a global router and returns the optimal route
        from start_waypoint to end_waypoint
        """
        # Setting up global router
        if self._grp is None:
            dao = GlobalRoutePlannerDAO(self._vehicle.get_world().get_map(), self._hop_resolution)
            grp = GlobalRoutePlanner(dao)
            grp.setup()
            self._grp = grp

        # Obtain route plan
        route = self._grp.trace_route(start_waypoint.transform.location, end_waypoint.transform.location)

        return route

    def run_step(self, debug=False):
        """
        Execute one step of navigation.
        :return: carla.VehicleControl
        """

        # obstacle flag
        hazard_detected = False

        # retrieve relevant elements for safe navigation, i.e.: traffic lights
        # and other vehicles
        actor_list = self._world.get_actors()
        vehicle_list = actor_list.filter("*vehicle*")
        lights_list = actor_list.filter("*traffic_light*")

        # Check possible obstacles in front
        obstacle_sensor = self._sensors["front_obstacle"]
        if obstacle_sensor.close_to_obstacle:
            if obstacle_sensor.distance_to_obstacle < self._safe_distance:
                vehicle = obstacle_sensor.obstacle
                if debug:
                    print('!!! VEHICLE BLOCKING AHEAD [{}])'.format(vehicle.id))

                self._state = AgentState.BLOCKED_BY_VEHICLE
                hazard_detected = True

        # check for the state of the traffic lights
        light_state, traffic_light = self._is_light_red(lights_list)
        if light_state:
            if debug:
                print('=== RED LIGHT AHEAD [{}])'.format(traffic_light.id))

            self._state = AgentState.BLOCKED_RED_LIGHT
            hazard_detected = True

        if hazard_detected:
            control = self.emergency_stop()
        else:
            self._state = AgentState.NAVIGATING
            # standard local planner behavior
            control = self._local_planner.run_step(debug=debug)

        return control

    def done(self):
        """
        Check whether the agent has reached its destination.
        :return bool
        """
        return self._local_planner.done()