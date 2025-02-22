#!/usr/bin/env python3

""" This is the starter code for the robot localization project """

from typing import List
import rclpy
from threading import Thread
from rclpy.time import Time
from rclpy.node import Node
from std_msgs.msg import Header
from sensor_msgs.msg import LaserScan
from geometry_msgs.msg import PoseWithCovarianceStamped, PoseArray, Pose, Point, Quaternion, Vector3
from visualization_msgs.msg import Marker
from rclpy.duration import Duration
import math
import time
import numpy as np
from occupancy_field import OccupancyField
from helper_functions import TFHelper, draw_random_sample
from rclpy.qos import qos_profile_sensor_data
from angle_helpers import quaternion_from_euler

class Particle(object):
    """ Represents a hypothesis (particle) of the robot's pose consisting of x,y and theta (yaw)
        Attributes:
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized
    """

    def __init__(self, x=0.0, y=0.0, theta=0.0, w=1.0):
        """ Construct a new Particle
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of KeyboardInterruptthe hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized """ 
        self.w = w
        self.theta = theta
        self.x = x
        self.y = y
    def __str__(self) -> str:
        return f"{self.x},{self.y},{self.theta},{self.w}"

    def as_pose(self):
        """ A helper function to convert a particle to a geometry_msgs/Pose message """
        q = quaternion_from_euler(0, 0, self.theta)
        return Pose(position=Point(x=self.x, y=self.y, z=0.0),
                    orientation=Quaternion(x=q[0], y=q[1], z=q[2], w=q[3]))

    def transform_points_particle_to_map_frame(self, points):
        rotation_mat = np.array([[np.cos(self.theta), -np.sin(self.theta)],[np.sin(self.theta), np.cos(self.theta)]])
        translation = np.array([self.x, self.y]).T
        return np.dot(rotation_mat, points.T) + translation.reshape((2,1))

class ParticleFilter(Node):
    """ The class that represents a Particle Filter ROS Node
        Attributes list:
            base_frame: the name of the robot base coordinate frame (should be "base_footprint" for most robots)
            map_frame: the name of the map coordinate frame (should be "map" in most cases)
            odom_frame: the name of the odometry coordinate frame (should be "odom" in most cases)
            scan_topic: the name of the scan topic to listen to (should be "scan" in most cases)
            n_particles: the number of particles in the filter
            d_thresh: the amount of linear movement before triggering a filter update
            a_thresh: the amount of angular movement before triggering a filter update
            pose_listener: a subscriber that listens for new approximate pose estimates (i.e. generated through the rviz GUI)
            particle_pub: a publisher for the particle cloud
            last_scan_timestamp: this is used to keep track of the clock when using bags
            scan_to_process: the scan that our run_loop should process next
            occupancy_field: this helper class allows you to query the map for distance to closest obstacle
            transform_helper: this helps with various transform operations (abstracting away the tf2 module)
            particle_cloud: a list of particles representing a probability distribution over robot poses
            current_odom_xy_theta: the pose of the robot in the odometry frame when the last filter update was performed.
                                   The pose is expressed as a list [x,y,theta] (where theta is the yaw)
            thread: this thread runs your main loop
    """
    def __init__(self):
        super().__init__('pf')
        self.base_frame = "base_footprint"   # the frame of the robot base
        self.map_frame = "map"          # the name of the map coordinate frame
        self.odom_frame = "odom"        # the name of the odometry coordinate frame
        self.scan_topic = "scan"        # the topic where we will get laser scans from 

        self.n_particles = 300          # the number of particles to use
        self.particle_decay_rate = 0.98

        self.d_thresh = 0.02             # the amount of linear movement before performing an update
        self.a_thresh = math.pi/6       # the amount of angular movement before performing an update

        self.xy_sigma = 0.07            # SD coefficient for x,y coordinates
        self.theta_sigma = 0.3          # SD deviation coefficient for theta value

        self.xy_sigma_odom = 0.1        # SD coefficient for the odom x,y coordinates
        self.theta_sigma_odom = 0.1     # SD coefficient for the odom theta value

        self.xy_sigma_init = 0.4        # SD coefficient for the initial x,y coordinates
        self.theta_sigma_init = 0.3     # SD coefficient for the initial theta value

        self.close_obs_dist = 0.01      # Initialize closest obsticale distance
        self.lidar_offset = -0.084      # Lidar posiiton on the NEATO. Set to 0 for the Turtlebot

        # pose_listener responds to selection of a new approximate robot location (for instance using rviz)
        self.create_subscription(PoseWithCovarianceStamped, 'initialpose', self.update_initial_pose, 10)

        # publish the current particle cloud.  This enables viewing particles in rviz.
        self.particle_pub = self.create_publisher(PoseArray, "particlecloud", qos_profile_sensor_data)

        self.scan_test_pub = self.create_publisher(Marker, "test", qos_profile_sensor_data)

        # laser_subscriber listens for data from the lidar
        self.create_subscription(LaserScan, self.scan_topic, self.scan_received, 10)

        # this is used to keep track of the timestamps coming from bag files
        # knowing this information helps us set the timestamp of our map -> odom
        # transform correctly
        self.last_scan_timestamp = None
        # this is the current scan that our run_loop should process
        self.scan_to_process = None
        # your particle cloud will go here
        self.particle_cloud : List[Particle] = []

        self.current_odom_xy_theta = []
        self.occupancy_field = OccupancyField(self)
        self.transform_helper = TFHelper(self)

        # we are using a thread to work around single threaded execution bottleneck
        thread = Thread(target=self.loop_wrapper)
        thread.start()
        self.transform_update_timer = self.create_timer(0.05, self.pub_latest_transform)

    def pub_latest_transform(self):
        """ This function takes care of sending out the map to odom transform """
        if self.last_scan_timestamp is None:
            return
        postdated_timestamp = Time.from_msg(self.last_scan_timestamp) + Duration(seconds=0.1)
        self.transform_helper.send_last_map_to_odom_transform(self.map_frame, self.odom_frame, postdated_timestamp)

    def loop_wrapper(self):
        """ This function takes care of calling the run_loop function repeatedly.
            We are using a separate thread to run the loop_wrapper to work around
            issues with single threaded executors in ROS2 """
        while True:
            self.run_loop()
            time.sleep(0.1)

    def run_loop(self):
        """ This is the main run_loop of our particle filter.  It checks to see if
            any scans are ready and to be processed and will call several helper
            functions to complete the processing.
            
            You do not need to modify this function, but it is helpful to understand it.
        """
        start = time.perf_counter()
        if self.scan_to_process is None:
            return
        msg = self.scan_to_process

        (new_pose, delta_t) = self.transform_helper.get_matching_odom_pose(self.odom_frame,
                                                                           self.base_frame,
                                                                           msg.header.stamp)
        if new_pose is None:
            # we were unable to get the pose of the robot corresponding to the scan timestamp
            if delta_t is not None and delta_t < Duration(seconds=0.0):
                # we will never get this transform, since it is before our oldest one
                self.scan_to_process = None
            return
        
        (r, theta) = self.transform_helper.convert_scan_to_polar_in_robot_frame(msg, self.base_frame)
        # print("r[0]={0}, theta[0]={1}".format(r[0], theta[0]))
        # clear the current scan so that we can process the next one
        self.scan_to_process = None

        self.odom_pose = new_pose
        new_odom_xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(self.odom_pose)
        # print("x: {0}, y: {1}, yaw: {2}".format(*new_odom_xy_theta))
        # print(f"START IF {time.perf_counter() - start}")
        if not self.current_odom_xy_theta:
            self.current_odom_xy_theta = new_odom_xy_theta
        elif not self.particle_cloud:
            # now that we have all of the necessary transforms we can update the particle cloud
            self.initialize_particle_cloud_kidnap()
            #  print(f"INIT {time.perf_counter() - start}")
        elif self.moved_far_enough_to_update(new_odom_xy_theta):
            # we have moved far enough to do an update!
            # print(f"BEFORE UPDATES {time.perf_counter() - start}")
            self.update_particles_with_odom()    # update based on odometry
            # print(f"ODOM {time.perf_counter() - start}")
            self.update_particles_with_laser(r, theta)   # update based on laser scan
            # print(f"LASER {time.perf_counter() - start}")
            self.update_robot_pose()                # update robot's pose based on particles
            # print(f"POSE {time.perf_counter() - start}")
            self.n_particles = int(self.n_particles*self.particle_decay_rate)
            self.resample_particles()               # resample particles to focus on areas of high density
            # print(f"RESAMPLE {time.perf_counter() - start}")
        # publish particles (so things like rviz can see them)
        self.publish_particles(msg.header.stamp)
        # print(f"PUBLISH {time.perf_counter() - start}")

    def moved_far_enough_to_update(self, new_odom_xy_theta):
        return math.fabs(new_odom_xy_theta[0] - self.current_odom_xy_theta[0]) > self.d_thresh or \
               math.fabs(new_odom_xy_theta[1] - self.current_odom_xy_theta[1]) > self.d_thresh or \
               math.fabs(new_odom_xy_theta[2] - self.current_odom_xy_theta[2]) > self.a_thresh


    def update_robot_pose(self):
        """ Update the estimate of the robot's pose given the updated particles.
            There are two logical methods for this:
                (1): compute the mean pose
                (2): compute the most likely pose (i.e. the mode of the distribution)
        """
        # first make sure that the particle weights are normalized
        self.normalize_particles()
        best_particle = self.particle_cloud[np.argmax([p.w for p in self.particle_cloud])]
        self.robot_pose = self.transform_helper.convert_translation_rotation_to_pose([best_particle.x,best_particle.y,0.0], quaternion_from_euler(0,0,best_particle.theta))
        self.transform_helper.fix_map_to_odom_transform(self.robot_pose,
                                                        self.odom_pose)

    def update_particles_with_odom(self):
        """ Update the particles using the newly given odometry pose.
            The function computes the value delta which is a tuple (x,y,theta)
            that indicates the change in position and angle between the odometry
            when the particles were last updated and the current odometry.
        """
        new_odom_xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(self.odom_pose)
        # compute the change in x,y,theta since our last update
        if self.current_odom_xy_theta:
            delta = (new_odom_xy_theta[0] - self.current_odom_xy_theta[0],
                     new_odom_xy_theta[1] - self.current_odom_xy_theta[1],
                     new_odom_xy_theta[2] - self.current_odom_xy_theta[2])

            self.current_odom_xy_theta = new_odom_xy_theta
        else:
            self.current_odom_xy_theta = new_odom_xy_theta
            return
        # Convert from map frame to robot frame 
        delta_x_n = np.cos(self.current_odom_xy_theta[2])*delta[0] + np.sin(self.current_odom_xy_theta[2])*delta[1]
        delta_y_n = -np.sin(self.current_odom_xy_theta[2])*delta[0] + np.cos(self.current_odom_xy_theta[2])*delta[1]
        for p in self.particle_cloud:
            # Apply transformation to particles
            p.x += np.cos(p.theta)*delta_x_n - np.sin(p.theta)*delta_y_n + self.xy_sigma_odom*np.random.randn()
            p.y += np.sin(p.theta)*delta_x_n + np.cos(p.theta)*delta_y_n + self.xy_sigma_odom*np.random.randn()
            p.theta = self.transform_helper.angle_normalize(p.theta + delta[2]) + self.theta_sigma_odom*np.random.randn()
            if not self.occupancy_field.is_free_position(p.x, p.y):
                self.particle_cloud.remove(p)

    def resample_particles(self):
        """ Resample the particles according to the new particle weights.
            The weights stored with each particle should define the probability that a particular
            particle is selected in the resampling step.  You may want to make use of the given helper
            function draw_random_sample in helper_functions.py.
        """
        # make sure the distribution is normalized
        self.normalize_particles()
        weights = [p.w for p in self.particle_cloud]\
        # get a random smaple of the particles
        random_sample : List[Particle] = draw_random_sample(self.particle_cloud, weights, self.n_particles)
        self.particle_cloud = []
        for p in random_sample: 
            # Resample particle values based on the new weights
            p.x = np.random.randn()*self.xy_sigma + p.x
            p.y = np.random.randn()*self.xy_sigma + p.y
            p.theta = self.transform_helper.angle_normalize(np.random.randn()*self.theta_sigma + p.theta)
            self.particle_cloud.append(p)

    def update_particles_with_laser(self, r, theta):
        """ Updates the particle weights in response to the scan data
            r: the distance readings to obstacles
            theta: the angle relative to the robot frame for each corresponding reading 
        """
        # transform laser to xy in particle neato frame
        scan_xy_n = np.array([np.cos(theta)*r, np.sin(theta)*r]).T + np.array([self.lidar_offset, 0])
        for p in self.particle_cloud:
            p.w = 0 # Reset particle weight
            scan_xy_p = p.transform_points_particle_to_map_frame(scan_xy_n).T
            closest_obs = self.occupancy_field.get_closest_obstacle_distance(scan_xy_p[:, 0], scan_xy_p[:, 1])
            p.w = (closest_obs < self.close_obs_dist).sum()

    def update_initial_pose(self, msg):
        """ Callback function to handle re-initializing the particle filter based on a pose estimate.
            These pose estimates could be generated by another ROS Node or could come from the rviz GUI """
        xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(msg.pose.pose)
        self.initialize_particle_cloud(msg.header.stamp, xy_theta)

    def initialize_particle_cloud_kidnap(self):
        """
        Initialize particle cloud into empty spaces on the map
        """
        self.particle_cloud = []
        # use n_random_free_point to occupy free space in the map
        points = self.occupancy_field.n_random_free_point(self.n_particles)
        for point in points:
            # randomize the particle placement
            x_noise = np.random.randn()*self.xy_sigma_init
            y_noise = np.random.randn()*self.xy_sigma_init
            theta = np.random.uniform(low=-np.pi, high=np.pi)
            self.particle_cloud.append(Particle(point[0] + x_noise, point[1]+y_noise, theta))

    def initialize_particle_cloud(self, timestamp, xy_theta=None):
        """ Initialize the particle cloud.
            Arguments
            xy_theta: a triple consisting of the mean x, y, and theta (yaw) to initialize the
                      particle cloud around.  If this input is omitted, the odometry will be used """
        if xy_theta is None:
            xy_theta = self.transform_helper.convert_pose_to_xy_and_theta(self.odom_pose)
        self.particle_cloud = []
        for _ in range(self.n_particles):
            x_noise = np.random.randn()*self.xy_sigma_init
            y_noise = np.random.randn()*self.xy_sigma_init
            theta_noise = np.random.randn()*self.theta_sigma_init
            x = xy_theta[0] + x_noise
            y = xy_theta[1] + y_noise
            theta = self.transform_helper.angle_normalize(xy_theta[2] + theta_noise)
            self.particle_cloud.append(Particle(x, y, theta, 1/self.n_particles))

        self.normalize_particles()

    def normalize_particles(self):
        """ Make sure the particle weights define a valid distribution (i.e. sum to 1.0) """
        weight_sum = sum([p.w for p in self.particle_cloud])
        for p in self.particle_cloud:
            if weight_sum == 0:
                p.w = 1/self.n_particles
            else:
                p.w /= weight_sum

    def publish_particles(self, timestamp):
        particles_conv = []
        for p in self.particle_cloud:
            particles_conv.append(p.as_pose())
        # actually send the message so that we can view it in rviz
        self.particle_pub.publish(PoseArray(header=Header(stamp=timestamp,
                                            frame_id=self.map_frame),
                                  poses=particles_conv))


    def scan_received(self, msg : LaserScan):
        self.last_scan_timestamp = msg.header.stamp
        # we throw away scans until we are done processing the previous scan
        # self.scan_to_process is set to None in the run_loop 
        if self.scan_to_process is None:
            self.scan_to_process = msg

def main(args=None):
    rclpy.init()
    n = ParticleFilter()
    rclpy.spin(n)
    rclpy.shutdown()

if __name__ == '__main__':
    main()
