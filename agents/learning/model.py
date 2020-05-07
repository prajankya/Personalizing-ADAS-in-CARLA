#!/usr/bin/env python
# coding=utf-8

import carla

import pickle
from os import path
from os.path import dirname, abspath

from scipy.interpolate import splrep
import matplotlib.pyplot as plt
import numpy as np
import math
from agents.tools.misc import get_poly_y, transform_to_frame


# A Model class to store personalized parameters
class Model:
    def __init__(self):
        # Path to store model
        self._data_folder = path.join(dirname(dirname(dirname(abspath(__file__)))), "data")
        self._model_path = path.join(self._data_folder, "model.pickle")
        # Model
        self._model = None
        self.load_model()

        # Learning data container
        self._distance_list = []
        self._velocity_list = []
        self._radar_list = []
        self._poly_points = []
        self._sin_points = []

    def load_model(self):
        # if file doesn't exist, create one
        if not path.exists(self._model_path):
            # Default model
            model = {"safe_distance": 10.0, "target_speed": 28.0,
                     "poly_param": {"lon_dis": 30.0, "lat_dis": -3.5, "dt": 4.0,
                                    "lon_param": np.array([0.0, 7.0, 0.0, 0.3125, -0.1172, 0.0117]),
                                    "lat_param": np.array([0.0, 0.0, 0.0, -0.5469, 0.2051, -0.0205])},
                     "sin_param": {"lon_dis": 30.0, "lat_dis": -3.5, "dt": 4.0}}
            with open(self._model_path, 'wb') as f:
                pickle.dump(model, f)

        # load model from file
        with open(self._model_path, 'rb') as f:
            model = pickle.load(f)
        self._model = model

    # Collect data
    def collect(self, dict_param):
        if dict_param:
            if "velocity" in dict_param:
                self._velocity_list.append(dict_param["velocity"])
            if "distance" in dict_param:
                self._distance_list.append(dict_param["distance"])
            if "points" in dict_param:
                self._poly_points.append(dict_param["points"])
                self._sin_points.append(dict_param["points"])
            if "radars" in dict_param:
                self._radar_list.append(dict_param["radars"])

    # Return a certain parameter value
    def get_parameter(self, keyword):
        return self._model[keyword]

    @staticmethod
    # update parameters according to found new one
    def change_param(current, new, max_change_rate=0.1):
        if abs(new - current) < max_change_rate * abs(current):
            result = new
        else:
            if new > current:
                result = current + max_change_rate * abs(current)
            else:
                result = current - max_change_rate * abs(current)
        return result

    @staticmethod
    # Return points indicating lane changing process
    def get_lane_changing_points(points):
        x_ref, y_ref, z_ref, yaw_ref, t_ref = points[0]

        # In matrix form
        points_m = np.transpose(np.array(points))

        # Transform back to (0, 0)
        ref_frame = carla.Transform(carla.Location(x=x_ref, y=y_ref, z=z_ref),
                                    carla.Rotation(yaw=yaw_ref, pitch=0, roll=0))
        points_v = transform_to_frame(ref_frame, points_m[0:3, :])

        # Get points (x, t) and (y, t)
        x_v = points_v[0,:]
        y_v = points_v[1,:]
        z_v = points_v[2,:]
        t_v = points_m[4,:] / 1000

        # Find out which part is lane changing
        e_y = y_v[1:] - y_v[0:-1]
        index = e_y < -0.02  # Left
        start_index = -1
        x_v = x_v[1:][index]
        y_v = y_v[1:][index]
        z_v = z_v[1:][index]
        t_v = t_v[1:][index]

        if x_v.shape[0] > 0:
            x_v -= x_v[0]
            y_v -= y_v[0]
            z_v -= z_v[0]
            t_v -= t_v[0]
            for i, val in enumerate(index):
                if val:
                    start_index = i
                    break

        #Temp Not using z_v for now
        return x_v, y_v, t_v, start_index

    # Learn from collected data to update value
    def update_poly_param(self, debug=False):
        """
        1, Polynomial curve definition comes from:
        S. Ammoun, F. Nashashibi and C. Laurgeau,
        "An analysis of the lane changing manoeuvre on roads :
        the contribution of inter-vehicle cooperation via communication," 2007
        IEEE Intelligent Vehicles Symposium, Istanbul, 2007, pp. 1095-1100.
        2, Learning method comes from:
        Yao, Wen & Zhao, Huijing & Davoine, Franck & Zha, Hongbin. (2012).
        Learning lane change trajectories from on-road driving data.
        IEEE Intelligent Vehicles Symposium, Proceedings. 885-890. 10.1109/IVS.2012.6232190.
        """
        if len(self._poly_points) < 50:
            return

        current_poly_param = self.get_parameter("poly_param")
        current_lon_param = current_poly_param["lon_param"]
        current_lat_param = current_poly_param["lat_param"]
        current_lon_dis = current_poly_param["lon_dis"]
        current_lat_dis = current_poly_param["lat_dis"]
        current_dt = current_poly_param["dt"]

        # Get points while lane changing
        x_v, y_v, t_v, start_index = self.get_lane_changing_points(self._poly_points)
        if len(x_v) < 10:
            return

        # New lane changing parameters
        new_dt = t_v[-1] - t_v[0]
        new_lon_dis = x_v[-1] - x_v[0]
        new_lat_dis = y_v[-1] - y_v[0]
        # update values
        dt = self.change_param(current_dt, new_dt)
        lon_dis = self.change_param(current_lon_dis, new_lon_dis)
        lat_dis = self.change_param(current_lat_dis, new_lat_dis)
        desired_lon_speed = lon_dis/dt

        # Calculate new polynomial parameters
        M = np.array([[1, 0, 0, 0, 0, 0],
                      [0, 1, 0, 0, 0, 0],
                      [0, 0, 2, 0, 0, 0],
                      [1, dt, dt ** 2, dt ** 3, dt ** 4, dt ** 5],
                      [0, 1, 2 * dt, 3 * dt ** 2, 4 * dt ** 3, 5 * dt ** 4],
                      [0, 0, 2, 6 * dt, 12 * dt ** 2, 20 * dt ** 3]])
        state_lon = np.array([0, desired_lon_speed, 0, lon_dis, desired_lon_speed, 0])
        state_lat = np.array([0, 0, 0, lat_dis, 0, 0])

        lon_param = np.matmul(np.linalg.inv(M), state_lon)
        lat_param = np.matmul(np.linalg.inv(M), state_lat)

        if debug:
            plt.figure(figsize=(3, 9))
            plt.subplot(312)
            plt.xlabel('time (t)')
            plt.ylabel('Longitudinal (m)')
            t_after = np.linspace(0, dt, 100)
            x_after = get_poly_y(t_after, lon_param)
            t_curr = np.linspace(0, current_dt, 100)
            x_curr = get_poly_y(t_curr, current_lon_param)
            plt.plot(t_after, x_after, color='g')
            plt.plot(t_curr, x_curr, color='y')
            plt.scatter(t_v, x_v, marker='x', color='k', s=5)
            plt.subplot(313)
            plt.xlabel('time (t)')
            plt.ylabel('Lateral (m)')
            y_after = get_poly_y(t_after, lat_param)
            y_curr = get_poly_y(t_curr, current_lat_param)
            plt.plot(t_after, y_after, color='g')
            plt.plot(t_curr, y_curr, color='y')
            plt.scatter(t_v, y_v, marker='x', color='k', s=5)
            plt.subplot(311)
            plt.xlabel('Longitudinal (m)')
            plt.ylabel('Lateral (m)')
            plt.plot(x_after, y_after, color='g')
            plt.plot(x_curr, y_curr, color='y')
            plt.scatter(x_v, y_v, marker='x', color='k', s=5)
            plt.show()

        # Update model
        poly_param = {"lat_dis": lat_dis, "lon_dis": lon_dis, "dt": dt,
                      "lat_param": lat_param, "lon_param": lon_param}
        self._model["poly_param"] = poly_param
        self._poly_points = []
        print("Poly Parameters Updated")

    def update_sin_param(self, debug=False):
        """
        Sinusoidal Method comes from:
        V. A. Butakov and P. Ioannou,
        "Personalized Driver/Vehicle Lane Change Models for ADAS,"
        in IEEE Transactions on Vehicular Technology, vol. 64, no. 10, pp. 4422-4431, Oct. 2015.
        """
        if len(self._sin_points) < 30:
            return  
        
        current_sin_param = self.get_parameter("sin_param")
        current_lon_dis = current_sin_param["lon_dis"]
        current_lat_dis = current_sin_param["lat_dis"]
        current_dt = current_sin_param["dt"]
        
        # Get points while lane changing
        x_v, y_v, t_v, start_index = self.get_lane_changing_points(self._sin_points)
        if len(x_v) < 10:
            return
        
        # New lane changing parameters
        new_dt = t_v[-1] - t_v[0]
        new_lon_dis = x_v[-1] - x_v[0]
        new_lat_dis = y_v[-1] - y_v[0]

        # For GMM
        # velocity of ego
        V = self._velocity_list[start_index].x
        # lateral distance
        H = new_lat_dis
        # get radar information when lane change happens
        radars = self._radar_list[start_index]
        DL = radars[1][1][0] if radars[1] else 100
        DF = radars[2][1][0] if radars[2] else -100
        print(V, H, DL, DF, new_dt)
        # Save data for GMM
        GMM_train_path = path.join(self._data_folder, "GMM_train_data.csv")
        if path.exists(GMM_train_path):
            with open(GMM_train_path, 'rb') as f:
                GMM_train_data = np.loadtxt(f, delimiter=",")
                GMM_train_data = np.atleast_2d(GMM_train_data)
            GMM_train_data = np.append(GMM_train_data, np.array([[V, H, DL, DF, new_dt]]), axis=0)
        else:
            GMM_train_data = np.array([[V, H, DL, DF, new_dt]])
        
        with open(GMM_train_path, 'wb') as f:
            np.savetxt(f, GMM_train_data, delimiter=",")

        # Update values
        dt = self.change_param(current_dt, new_dt)
        lon_dis = self.change_param(current_lon_dis, new_lon_dis)
        lat_dis = self.change_param(current_lat_dis, new_lat_dis)

        if debug:
            t_after = np.linspace(0, dt, 100)
            t_curr = np.linspace(0, current_dt, 100)
            plt.figure(figsize=(3, 9))
            plt.subplot(312)
            plt.xlabel('time (t)')
            plt.ylabel('Longitudinal (m)')
            x_after = np.linspace(0, lon_dis, 100)
            x_curr = np.linspace(0, current_lon_dis, 100)
            plt.plot(t_after, x_after, color='g')
            plt.plot(t_curr, x_curr, color='y')
            plt.scatter(t_v, x_v, marker='x', color='k', s=5)
            plt.subplot(313)
            plt.xlabel('time (t)')
            plt.ylabel('Lateral (m)')
            y_after = -lat_dis/(2*math.pi) * np.sin(2*math.pi * t_after/dt) + lat_dis * t_after/dt
            y_curr = -current_lat_dis/(2*math.pi)*np.sin(2*math.pi*t_curr/current_dt)+current_lat_dis*t_curr/current_dt
            plt.plot(t_after, y_after, color='g')
            plt.plot(t_curr, y_curr, color='y')
            plt.scatter(t_v, y_v, marker='x', color='k', s=5)
            plt.subplot(311)
            plt.xlabel('Longitudinal (m)')
            plt.ylabel('Lateral (m)')
            plt.plot(x_after, y_after, color='g')
            plt.plot(x_curr, y_curr, color='y')
            plt.scatter(x_v, y_v, marker='x', color='k', s=5)
            plt.show()

        # Update model
        sin_param = {"lat_dis": lat_dis, "lon_dis": lon_dis, "dt": dt}
        self._model["sin_param"] = sin_param
        self._sin_points = []
        print("Sinusoidal Parameters Updated")

    def update_safe_distance(self, debug=False):
        # Only consider the lowest 10% of the distance value list
        percentage = 0.2
        length = int(percentage * len(self._distance_list))
        if length <= 5:
            return

        current_dist = self.get_parameter("safe_distance")

        # get new value
        self._distance_list.sort()
        new_dist = sum(self._distance_list[0:length]) / length
        # update
        safe_distance = self.change_param(current_dist, new_dist)

        self._model["safe_distance"] = safe_distance
        self._distance_list = []
        print("Safe Distance Updated")

    def update_target_speed(self):
        # Select ones while going straight
        speed_straight_list = []
        for v in self._velocity_list:
            if abs(v.x) > 4 and abs(v.x) / abs(v.y) > 30:
                speed = 3.6 * v.x # km/h
                speed_straight_list.append(speed)

        # Only consider the largest 10% of the speed value list
        percentage = 0.2
        length = int(percentage * len(speed_straight_list))
        if length <= 10:
            return

        current_speed = self.get_parameter("target_speed")

        # get new value
        speed_straight_list.sort(reverse=True)
        new_speed = sum(speed_straight_list[0:length]) / length
        # update
        target_speed = self.change_param(current_speed, new_speed)

        self._model["target_speed"] = target_speed
        self._velocity_list = []
        print("Target Speed Updated")

    # Store learned result
    def store_new_model(self):
        with open(self._model_path, 'wb') as f:
            pickle.dump(self._model, f)

    # End collecting data
    def end_collect(self):
        '''
        self.update_sin_param(debug=True)
        self.update_poly_param()
        self.update_safe_distance()
        self.update_target_speed()
        '''

        self.store_new_model()
