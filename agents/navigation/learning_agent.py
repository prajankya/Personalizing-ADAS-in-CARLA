#!/usr/bin/env python

import pygame
import carla
import math

from agents.navigation.agent import Agent, AgentState
from agents.navigation.local_planner import LocalPlanner
from agents.navigation.global_route_planner import GlobalRoutePlanner
from agents.navigation.global_route_planner_dao import GlobalRoutePlannerDAO
from agents.navigation.lange_change import PolyLaneChange
from agents.learning.model import Model


class LearningAgent(Agent):
    """
    BasicAgent implements a basic agent that navigates scenes to reach a given
    target destination. This agent respects traffic lights and other vehicles.
    """

    def __init__(self, world):
        """
        :param vehicle: actor to apply to local planner logic onto
        """
        super(LearningAgent, self).__init__(world.player)
        self._world_obj = world

        self._model = Model()
        self._safe_distance = None
        self._target_speed = None
        self._local_planner = LocalPlanner(world.player)
        self._poly_param = None
        self.update_parameters()

        self._block_count = 0  # Temporary

        self._proximity_threshold = 10.0  # meters
        self._state = AgentState.NAVIGATING
        self._hop_resolution = 2.0
        self._path_seperation_hop = 2
        self._path_seperation_threshold = 0.5

        self._grp = None  # global route planar

    # Update personalized parameters from model
    def update_parameters(self):
        self._safe_distance = self._model.get_parameter("safe_distance")
        self._target_speed = self._model.get_parameter("target_speed")
        self._poly_param = self._model.get_parameter("poly_param")
        args_lateral_dict = {'K_P': 1.0, 'K_I': 0.4, 'K_D': 0.01}
        args_longitudinal_dict = {'K_P': 0.3, 'K_I': 0.2, 'K_D': 0.002}
        self._local_planner.init_controller(opt_dict={'target_speed': self._target_speed,
                                                      'lateral_control_dict': args_lateral_dict,
                                                      'longitudinal_control_dict': args_longitudinal_dict})

    # Start learning by collecting data
    def collect(self):
        dict_param = {}
        # Collect points while lane changing
        dict_param.update({"poly_points": [self._vehicle.get_location().x,
                                           self._vehicle.get_location().y,
                                           self._vehicle.get_transform().rotation.yaw,
                                           pygame.time.get_ticks()]})

        # Collect speed while going straight
        v = self._world_obj.player.get_velocity()
        if abs(v.x) > 4 and abs(v.x)/abs(v.y) > 30:
            speed = 3.6 * math.sqrt(v.x ** 2 + v.y ** 2 + v.z ** 2)
            dict_param.update({"speed": speed})

        # Collect distance to obstacle in front
        if self._world_obj.obstacle_sensor.close_to_obstacle:
            dict_param.update({"distance": self._world_obj.obstacle_sensor.distance_to_obstacle})

        self._model.collect(dict_param)

    # End learning mode
    def end_collect(self):
        self._model.end_collect()
        self.update_parameters()

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

    def empty_control(self):
        """
        Send an light stop command to the vehicle
        :return:
        """
        control = carla.VehicleControl()
        control.steer = 0.0
        control.throttle = 0.0
        control.brake = 0.0
        control.hand_brake = False

        return control

    def soft_stop(self):
        """
        Send an light stop command to the vehicle
        :return:
        """
        control = carla.VehicleControl()
        control.steer = 0.0
        control.throttle = 0.0
        control.brake = 0.1
        control.hand_brake = False

        return control

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
        lights_list = actor_list.filter("*traffic_light*")

        # Check possible obstacles in front
        if self._world_obj.obstacle_sensor.close_to_obstacle:
            if self._world_obj.obstacle_sensor.distance_to_obstacle < self._safe_distance:
                vehicle = self._world_obj.obstacle_sensor.obstacle
                if debug:
                    print('Vehicle ahead [{}] for {} seconds)'.format(vehicle.id, self._block_count / 20.0))

                self._state = AgentState.BLOCKED_BY_VEHICLE
                hazard_detected = True

        '''
        # check for the state of the traffic lights
        light_state, traffic_light = self._is_light_red(lights_list)
        if light_state:
            if debug:
                print('=== RED LIGHT AHEAD [{}])'.format(traffic_light.id))
            self._state = AgentState.BLOCKED_RED_LIGHT
            hazard_detected = True
        '''

        if hazard_detected:
            self._local_planner.update_buffer()
            control = self.empty_control()
            self._block_count += 1
        else:
            self._state = AgentState.NAVIGATING
            # standard local planner behavior
            control = self._local_planner.run_step(debug=debug)

        # When accumulated block time is larger than 3 seconds
        if self._block_count > 60:
            # Record original destination
            destination = self._local_planner.get_global_destination()
            # Get lane change start location
            ref_location = self._world_obj.player.get_location()
            ref_yaw = self._world_obj.player.get_transform().rotation.yaw
            if self._local_planner.waypoint_buffer:
                waypoint = self._local_planner.waypoint_buffer[-1][0]
                ref_location = waypoint.transform.location
            wait_dist = 3.0  # need some time to plan
            ref = [ref_location.x + wait_dist, ref_location.y, ref_yaw]

            # Replace current plan with a lane change plan
            lane_changer = PolyLaneChange(self._world_obj, self._poly_param)
            lane_change_plan = lane_changer.get_waypoints(self._target_speed, ref)
            self._local_planner.set_local_plan(lane_change_plan)

            # Replan with new vehicle position
            new_start = self._map.get_waypoint(lane_change_plan[-1][0].transform.location)
            route_trace = self._trace_route(new_start, destination)
            assert route_trace
            self._local_planner.add_global_plan(route_trace)

            print("perform lane change")
            self._block_count = 0

        return control

    def done(self):
        """
        Check whether the agent has reached its destination.
        :return bool
        """
        return self._local_planner.done()
